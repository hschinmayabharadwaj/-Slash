package ai

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
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
