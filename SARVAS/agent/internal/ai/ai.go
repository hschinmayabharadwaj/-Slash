// Package ai talks to the model provider. Gemini is called over its REST API
// with the standard library, so no SDK runs inside the sandbox.
package ai

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
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

func (g *Gemini) Generate(ctx context.Context, prompt string, jsonMode bool) (string, int, error) {
	if g.APIKey == "" {
		return "", 0, errors.New("GEMINI_API_KEY is not set")
	}
	model := g.Model
	if model == "" {
		model = "gemini-2.5-flash"
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
	req, err := http.NewRequestWithContext(ctx, "POST",
		fmt.Sprintf("%s/v1beta/models/%s:generateContent", base, model), bytes.NewReader(body))
	if err != nil {
		return "", 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-goog-api-key", g.APIKey) // header, never the URL (keeps it out of logs)
	client := g.HTTP
	if client == nil {
		client = &http.Client{Timeout: 120 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", 0, errors.New("could not reach the Gemini API")
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
		return "", 0, errors.New("could not reach the Ollama server")
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

// New returns the configured provider, or nil for the offline "sim" provider.
func New(provider, apiKey, model, baseURL string) Provider {
	switch provider {
	case "gemini":
		return &Gemini{APIKey: apiKey, Model: model}
	case "ollama":
		return &Ollama{BaseURL: baseURL, Model: model}
	}
	return nil
}
