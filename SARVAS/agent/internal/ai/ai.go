// Package ai talks to the model provider. Every provider is called over its
// REST API with the standard library, so no SDK runs inside the sandbox.
package ai

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Provider generates one completion. JSONMode asks for a JSON-only reply.
type Provider interface {
	Name() string
	Generate(ctx context.Context, prompt string, jsonMode bool) (text string, tokens int, err error)
}

// Gemini calls generativelanguage.googleapis.com.
type Gemini struct {
	APIKey  string
	Model   string
	BaseURL string // tests
	HTTP    *http.Client
}

func (g *Gemini) Name() string { return "gemini" }

// backoff is the wait before each retry of an overloaded request (tests shorten it).
var backoff = []time.Duration{2 * time.Second, 5 * time.Second, 10 * time.Second, 20 * time.Second}

func retryable(status int) bool {
	switch status {
	case http.StatusTooManyRequests, http.StatusInternalServerError, http.StatusBadGateway,
		http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	}
	return false
}

func (g *Gemini) Generate(ctx context.Context, prompt string, jsonMode bool) (string, int, error) {
	if g.APIKey == "" {
		return "", 0, errors.New("GEMINI_API_KEY is not set")
	}
	model := g.Model
	if model == "" {
		model = "gemini-3.8-flash"
	}
	base := g.BaseURL
	if base == "" {
		base = "https://generativelanguage.googleapis.com"
	}
	genCfg := map[string]any{"temperature": 0.2, "maxOutputTokens": 16384}
	if jsonMode {
		genCfg["responseMimeType"] = "application/json"
	}
	body, _ := json.Marshal(map[string]any{
		"contents":         []any{map[string]any{"role": "user", "parts": []any{map[string]string{"text": prompt}}}},
		"generationConfig": genCfg,
	})
	client := g.HTTP
	if client == nil {
		// The caller's context sets the real limit (agent.planTimeoutS etc.).
		client = &http.Client{Timeout: 10 * time.Minute}
	}
	h := http.Header{}
	h.Set("Content-Type", "application/json")
	h.Set("x-goog-api-key", g.APIKey) // header, never the URL (keeps it out of logs)
	resp, err := post(ctx, client, fmt.Sprintf("%s/v1beta/models/%s:generateContent", base, model), "the Gemini API", body, h)
	if err != nil {
		return "", 0, err
	}
	defer resp.Body.Close()
	var out struct {
		Candidates []struct {
			Content struct {
				Parts []struct {
					Text string `json:"text"`
				} `json:"parts"`
			} `json:"content"`
		} `json:"candidates"`
		UsageMetadata struct {
			TotalTokenCount int `json:"totalTokenCount"`
		} `json:"usageMetadata"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", 0, fmt.Errorf("unreadable Gemini response (HTTP %d)", resp.StatusCode)
	}
	if resp.StatusCode >= 300 || out.Error != nil {
		msg := ""
		if out.Error != nil {
			msg = out.Error.Message
		}
		if len(msg) > 200 {
			msg = msg[:200]
		}
		return "", 0, fmt.Errorf("Gemini API error (HTTP %d): %s", resp.StatusCode, msg)
	}
	var b strings.Builder
	if len(out.Candidates) > 0 {
		for _, p := range out.Candidates[0].Content.Parts {
			b.WriteString(p.Text)
		}
	}
	return b.String(), out.UsageMetadata.TotalTokenCount, nil
}

// Ollama calls a self-hosted Ollama server, so prompts (and any repository
// code in them) stay on the operator's infrastructure.
type Ollama struct {
	BaseURL string // e.g. http://127.0.0.1:11434
	Model   string
	HTTP    *http.Client
}

func (o *Ollama) Name() string { return "ollama" }

func (o *Ollama) Generate(ctx context.Context, prompt string, jsonMode bool) (string, int, error) {
	if o.BaseURL == "" || o.Model == "" {
		return "", 0, errors.New("ollama needs ai.baseUrl and ai.model")
	}
	payload := map[string]any{
		"model":    o.Model,
		"messages": []any{map[string]string{"role": "user", "content": prompt}},
		"stream":   false,
		"options":  map[string]any{"temperature": 0.2},
	}
	if jsonMode {
		payload["format"] = "json"
	}
	body, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(o.BaseURL, "/")+"/api/chat", bytes.NewReader(body))
	if err != nil {
		return "", 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	client := o.HTTP
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Minute} // local models can be slow
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", 0, reachError("the Ollama server", err)
	}
	defer resp.Body.Close()
	var out struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
		PromptEvalCount int    `json:"prompt_eval_count"`
		EvalCount       int    `json:"eval_count"`
		Error           string `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", 0, fmt.Errorf("unreadable Ollama response (HTTP %d)", resp.StatusCode)
	}
	if resp.StatusCode >= 300 || out.Error != "" {
		msg := out.Error
		if len(msg) > 200 {
			msg = msg[:200]
		}
		return "", 0, fmt.Errorf("Ollama error (HTTP %d): %s", resp.StatusCode, msg)
	}
	return out.Message.Content, out.PromptEvalCount + out.EvalCount, nil
}

// OpenAI calls any OpenAI-compatible /chat/completions API: Baseten,
// Fireworks, a vLLM server on Modal, OpenRouter, Groq and so on.
type OpenAI struct {
	BaseURL string // up to and including /v1, e.g. https://api.fireworks.ai/inference/v1
	APIKey  string // optional for a private server
	Model   string
	HTTP    *http.Client
}

func (o *OpenAI) Name() string { return "openai" }

func (o *OpenAI) Generate(ctx context.Context, prompt string, jsonMode bool) (string, int, error) {
	if o.BaseURL == "" || o.Model == "" {
		return "", 0, errors.New("the openai provider needs ai.baseUrl and ai.model")
	}
	payload := map[string]any{
		"model":       o.Model,
		"messages":    []any{map[string]string{"role": "user", "content": prompt}},
		"temperature": 0.2,
		"max_tokens":  16384,
		// Streaming lifts Fireworks' 4096-token cap on plain requests and keeps
		// long answers alive behind proxies that cut idle connections.
		"stream":         true,
		"stream_options": map[string]any{"include_usage": true},
	}
	if jsonMode {
		payload["response_format"] = map[string]string{"type": "json_object"}
	}
	body, _ := json.Marshal(payload)
	h := http.Header{}
	h.Set("Content-Type", "application/json")
	if o.APIKey != "" {
		h.Set("Authorization", "Bearer "+o.APIKey)
	}
	client := o.HTTP
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Minute} // the caller's context sets the real limit
	}
	resp, err := post(ctx, client, strings.TrimRight(o.BaseURL, "/")+"/chat/completions", "the model API", body, h)
	if err != nil {
		return "", 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		msg := strings.TrimSpace(string(raw))
		var e struct {
			Error *struct {
				Message string `json:"message"`
			} `json:"error"`
			Message string `json:"message"` // vLLM's shape
		}
		if json.Unmarshal(raw, &e) == nil {
			if e.Error != nil {
				msg = e.Error.Message
			} else if e.Message != "" {
				msg = e.Message
			}
		}
		if len(msg) > 200 {
			msg = msg[:200]
		}
		return "", 0, fmt.Errorf("model API error (HTTP %d): %s", resp.StatusCode, msg)
	}
	var b strings.Builder
	tokens := 0
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for sc.Scan() {
		data, ok := strings.CutPrefix(sc.Text(), "data:")
		data = strings.TrimSpace(data)
		if !ok || data == "[DONE]" {
			continue
		}
		var chunk struct {
			Choices []struct {
				Delta struct {
					Content string `json:"content"`
				} `json:"delta"`
			} `json:"choices"`
			Usage *struct {
				TotalTokens int `json:"total_tokens"`
			} `json:"usage"`
			Error *struct {
				Message string `json:"message"`
			} `json:"error"`
		}
		if json.Unmarshal([]byte(data), &chunk) != nil {
			continue
		}
		if chunk.Error != nil {
			return "", 0, fmt.Errorf("model API error: %s", chunk.Error.Message)
		}
		for _, c := range chunk.Choices {
			b.WriteString(c.Delta.Content)
		}
		if chunk.Usage != nil {
			tokens = chunk.Usage.TotalTokens
		}
	}
	if err := sc.Err(); err != nil {
		return "", 0, reachError("the model API", err)
	}
	if tokens == 0 {
		tokens = (len(prompt) + b.Len()) / 4 // shortcut: ~4 chars/token when the server reports no usage, fine for budgets
	}
	return b.String(), tokens, nil
}

// post sends body, retrying overload and rate-limit answers, which are usually brief.
func post(ctx context.Context, client *http.Client, url, what string, body []byte, h http.Header) (*http.Response, error) {
	for attempt := 0; ; attempt++ {
		req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header = h.Clone()
		resp, err := client.Do(req)
		if err != nil {
			return nil, reachError(what, err)
		}
		if !retryable(resp.StatusCode) || attempt == len(backoff) {
			return resp, nil
		}
		resp.Body.Close()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(backoff[attempt]):
		}
	}
}

// New returns the configured provider, or nil for the offline "sim" provider.
func New(provider, apiKey, model, baseURL string) Provider {
	switch provider {
	case "gemini":
		return &Gemini{APIKey: apiKey, Model: model}
	case "ollama":
		return &Ollama{BaseURL: baseURL, Model: model}
	case "openai":
		return &OpenAI{BaseURL: baseURL, APIKey: apiKey, Model: model}
	}
	return nil
}

// reachError says why a request never got an answer. The key travels in a
// header, so the wrapped URL error is safe to show.
func reachError(what string, err error) error {
	var ne net.Error
	if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &ne) && ne.Timeout()) {
		return fmt.Errorf("%s didn't answer in time; try again, use a faster model, or raise agent.planTimeoutS/implementTimeoutS in config.json", what)
	}
	if ue, ok := err.(*url.Error); ok {
		err = ue.Err
	}
	return fmt.Errorf("could not reach %s: %v", what, err)
}
