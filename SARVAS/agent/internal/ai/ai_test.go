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
	saved := geminiBackoff
	geminiBackoff = []time.Duration{0, 0}
	defer func() { geminiBackoff = saved }()

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
}
