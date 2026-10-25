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

// geminiBackoff is the wait before each retry of an overloaded request (tests shorten it).
var geminiBackoff = []time.Duration{2 * time.Second, 5 * time.Second, 10 * time.Second, 20 * time.Second}

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
	client := g.HTTP
	if client == nil {
		client = &http.Client{Timeout: 120 * time.Second}
	}
	// Overload and rate-limit answers are usually brief, so retry them with backoff.
	var resp *http.Response
	for attempt := 0; ; attempt++ {
		req, err := http.NewRequestWithContext(ctx, "POST",
			fmt.Sprintf("%s/v1beta/models/%s:generateContent", base, model), bytes.NewReader(body))
		if err != nil {
			return "", 0, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("x-goog-api-key", g.APIKey) // header, never the URL (keeps it out of logs)
		resp, err = client.Do(req)
		if err != nil {
			return "", 0, errors.New("could not reach the Gemini API")
		}
		if !retryable(resp.StatusCode) || attempt == len(geminiBackoff) {
			break
		}
		resp.Body.Close()
		select {
		case <-ctx.Done():
			return "", 0, ctx.Err()
		case <-time.After(geminiBackoff[attempt]):
		}
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
