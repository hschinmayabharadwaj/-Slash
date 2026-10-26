package ai

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestGeminiRequestShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.String(), "AIza") || r.Header.Get("x-goog-api-key") != "AIzaKEY12345" {
			t.Errorf("key must be in the header only: %s", r.URL)
		}
		if !strings.HasSuffix(r.URL.Path, "/models/gemini-x:generateContent") {
			t.Errorf("path %s", r.URL.Path)
		}
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if body["generationConfig"].(map[string]any)["responseMimeType"] != "application/json" {
			t.Error("json mode not requested")
		}
		w.Write([]byte(`{"candidates":[{"content":{"parts":[{"text":"{\"a\":"},{"text":"1}"}]}}],"usageMetadata":{"totalTokenCount":42}}`))
	}))
	defer srv.Close()
	g := &Gemini{APIKey: "AIzaKEY12345", Model: "gemini-x", BaseURL: srv.URL}
	text, tokens, err := g.Generate(context.Background(), "hi", true)
	if err != nil || text != `{"a":1}` || tokens != 42 {
		t.Fatalf("%q %d %v", text, tokens, err)
	}
}

func TestGeminiErrorDoesNotLeakKey(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(400)
		w.Write([]byte(`{"error":{"message":"API key not valid"}}`))
	}))
	defer srv.Close()
	_, _, err := (&Gemini{APIKey: "AIzaKEY12345", BaseURL: srv.URL}).Generate(context.Background(), "hi", false)
	if err == nil || strings.Contains(err.Error(), "AIzaKEY12345") {
		t.Fatalf("%v", err)
	}
}

func TestGeminiRetriesOverload(t *testing.T) {
	saved := backoff
	backoff = []time.Duration{0, 0}
	defer func() { backoff = saved }()

	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		if calls < 3 {
			w.WriteHeader(503)
			w.Write([]byte(`{"error":{"message":"high demand"}}`))
			return
		}
		w.Write([]byte(`{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}`))
	}))
	defer srv.Close()
	text, _, err := (&Gemini{APIKey: "k", BaseURL: srv.URL}).Generate(context.Background(), "hi", false)
	if err != nil || text != "ok" || calls != 3 {
		t.Fatalf("text=%q calls=%d err=%v", text, calls, err)
	}

	calls = -10 // always overloaded: gives up after the last backoff
	_, _, err = (&Gemini{APIKey: "k", BaseURL: srv.URL}).Generate(context.Background(), "hi", false)
	if err == nil || !strings.Contains(err.Error(), "HTTP 503") || calls != -7 {
		t.Fatalf("calls=%d err=%v", calls, err)
	}
}

func TestOllamaRequestShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/chat" {
			t.Errorf("path %s", r.URL.Path)
		}
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if body["model"] != "qwen2.5-coder:7b" || body["format"] != "json" || body["stream"] != false {
			t.Errorf("body %v", body)
		}
		w.Write([]byte(`{"message":{"role":"assistant","content":"{\"ok\":true}"},"prompt_eval_count":10,"eval_count":5}`))
	}))
	defer srv.Close()
	o := &Ollama{BaseURL: srv.URL + "/", Model: "qwen2.5-coder:7b"}
	text, tokens, err := o.Generate(context.Background(), "hi", true)
	if err != nil || text != `{"ok":true}` || tokens != 15 {
		t.Fatalf("%q %d %v", text, tokens, err)
	}
}

func TestOllamaError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(404)
		w.Write([]byte(`{"error":"model \"nope\" not found, try pulling it first"}`))
	}))
	defer srv.Close()
	_, _, err := (&Ollama{BaseURL: srv.URL, Model: "nope"}).Generate(context.Background(), "hi", false)
	if err == nil || !strings.Contains(err.Error(), "not found") {
		t.Fatalf("%v", err)
	}
}

func TestNewProvider(t *testing.T) {
	if New("sim", "", "", "") != nil {
		t.Error("sim must be nil (offline)")
	}
	if p := New("ollama", "", "m", "http://x"); p == nil || p.Name() != "ollama" {
		t.Error("ollama")
	}
	if p := New("gemini", "k", "m", ""); p == nil || p.Name() != "gemini" {
		t.Error("gemini")
	}
	if p := New("openai", "k", "m", "https://x/v1"); p == nil || p.Name() != "openai" {
		t.Error("openai")
	}
}

// Baseten, Fireworks and vLLM-on-Modal all speak this streaming shape.
func TestOpenAIStreams(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" || r.Header.Get("Authorization") != "Bearer fw_SECRET" {
			t.Errorf("%s %q", r.URL.Path, r.Header.Get("Authorization"))
		}
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if body["model"] != "accounts/fireworks/models/qwen3-coder" || body["stream"] != true || body["response_format"] == nil {
			t.Errorf("body %v", body)
		}
		w.Write([]byte("data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\n" +
			"data: {\"choices\":[{\"delta\":{\"content\":\"{\\\"ok\\\"\"}}]}\n\n" +
			": keep-alive\n\n" +
			"data: {\"choices\":[{\"delta\":{\"content\":\":true}\"}}]}\n\n" +
			"data: {\"choices\":[],\"usage\":{\"total_tokens\":42}}\n\n" +
			"data: [DONE]\n\n"))
	}))
	defer srv.Close()
	o := &OpenAI{BaseURL: srv.URL + "/v1/", APIKey: "fw_SECRET", Model: "accounts/fireworks/models/qwen3-coder"}
	text, tokens, err := o.Generate(context.Background(), "hi", true)
	if err != nil || text != `{"ok":true}` || tokens != 42 {
		t.Fatalf("%q %d %v", text, tokens, err)
	}
}

func TestOpenAIError(t *testing.T) {
	for _, body := range []string{
		`{"error":{"message":"Model not found, inaccessible, and/or not deployed"}}`, // Fireworks/OpenAI
		`{"object":"error","message":"Model not found"}`,                             // vLLM
	} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(404)
			w.Write([]byte(body))
		}))
		_, _, err := (&OpenAI{BaseURL: srv.URL, APIKey: "fw_SECRET", Model: "nope"}).Generate(context.Background(), "hi", false)
		srv.Close()
		if err == nil || !strings.Contains(err.Error(), "Model not found") || strings.Contains(err.Error(), "fw_SECRET") {
			t.Fatalf("%v", err)
		}
	}
}

func TestUnreachableModelSaysWhy(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(200 * time.Millisecond)
	}))
	defer srv.Close()
	g := &Gemini{APIKey: "AIzaKEY12345", Model: "m", BaseURL: srv.URL, HTTP: &http.Client{Timeout: 50 * time.Millisecond}}
	if _, _, err := g.Generate(context.Background(), "hi", false); err == nil || !strings.Contains(err.Error(), "didn't answer in time") {
		t.Fatalf("timeout not explained: %v", err)
	}
	g.BaseURL = "http://127.0.0.1:1"
	if _, _, err := g.Generate(context.Background(), "hi", false); err == nil || !strings.Contains(err.Error(), "could not reach the Gemini API: ") || strings.Contains(err.Error(), "AIza") {
		t.Fatalf("bad error: %v", err)
	}
}
