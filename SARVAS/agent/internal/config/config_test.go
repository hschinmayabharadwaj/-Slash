package config

import (
	"os"
	"path/filepath"
	"testing"
)

func write(t *testing.T, body string) string {
	p := filepath.Join(t.TempDir(), "config.json")
	os.WriteFile(p, []byte(body), 0o600)
	return p
}

func TestEnvExpansionAndDefaults(t *testing.T) {
	for _, k := range []string{"AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL", "AI_SEND_CODE"} {
		t.Setenv(k, "")
	}
	t.Setenv("MY_KEY", "AIzaSyExampleKey123")
	cfg, err := Load(write(t, `{"ai":{"provider":"gemini","apiKey":"${MY_KEY}"},"repos":{"o/r":{"testCommand":"go test ./..."}}}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.AI.APIKey != "AIzaSyExampleKey123" || cfg.Security.ChecksNetwork != "none" || cfg.Repo("o/r").DefaultBranch != "main" {
		t.Fatalf("%+v", cfg)
	}
}

func TestValidation(t *testing.T) {
	// make exports .env, which would fill these in and make the
	// intentionally-invalid fixtures below valid.
	for _, k := range []string{"AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL", "AI_SEND_CODE"} {
		t.Setenv(k, "")
	}
	for _, body := range []string{
		`{"ai":{"provider":"gemini"}}`,
		`{"ai":{"provider":"openai","model":"m"}}`,
		`{"ai":{"provider":"other"}}`,
		`{"ai":{"provider":"sim"},"security":{"checksNetwork":"host"}}`,
	} {
		if _, err := Load(write(t, body)); err == nil {
			t.Errorf("accepted %s", body)
		}
	}
}

func TestRepoAllowlist(t *testing.T) {
	off := false
	cfg := Default()
	cfg.Security.AllowedRepos = []string{"Org/Allowed"}
	cfg.Repos["org/allowed2"] = Repo{Enabled: &off}
	if !cfg.RepoAllowed("org/allowed") || cfg.RepoAllowed("org/other") || cfg.RepoAllowed("org/allowed2") {
		t.Fatal("allowlist")
	}
}

func TestListsFromEnvStrings(t *testing.T) {
	for _, k := range []string{"AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL", "AI_SEND_CODE"} {
		t.Setenv(k, "")
	}
	path := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("REPOS", " acme/web , acme/api,,")
	os.WriteFile(path, []byte(`{"ai":{"provider":"sim"},"security":{"allowedRepos":"${REPOS}"}}`), 0o600)
	cfg, err := Load(path)
	if err != nil || len(cfg.Security.AllowedRepos) != 2 || !cfg.RepoAllowed("ACME/api") || cfg.RepoAllowed("acme/other") {
		t.Fatalf("%v %v", cfg.Security.AllowedRepos, err)
	}
	t.Setenv("REPOS", "")
	if cfg, _ := Load(path); len(cfg.Security.AllowedRepos) != 0 || !cfg.RepoAllowed("any/repo") {
		t.Fatal("empty env must mean no allowlist")
	}
}

func TestOllamaNeedsAModel(t *testing.T) {
	t.Setenv("AI_PROVIDER", "") // make exports .env, which would fill these in
	t.Setenv("AI_MODEL", "")
	t.Setenv("AI_BASE_URL", "")
	path := filepath.Join(t.TempDir(), "config.json")
	os.WriteFile(path, []byte(`{"ai":{"provider":"ollama"}}`), 0o600)
	if _, err := Load(path); err == nil {
		t.Fatal("ollama without a model must be rejected")
	}
	os.WriteFile(path, []byte(`{"ai":{"provider":"ollama","model":"qwen2.5-coder:7b"}}`), 0o600)
	cfg, err := Load(path)
	if err != nil || cfg.AI.BaseURL == "" || cfg.AI.Remote() || !cfg.AI.CodeAllowed() {
		t.Fatalf("%+v %v", cfg.AI, err)
	}
}

func TestRemoteProviderNeedsConsentForCode(t *testing.T) {
	ai := AI{Provider: "gemini"}
	if ai.CodeAllowed() {
		t.Fatal("remote provider must not get code without consent")
	}
	ai.SendCode = true
	if !ai.CodeAllowed() {
		t.Fatal("consent ignored")
	}
}

func TestCommitEmailCannotBeClaimedByAStranger(t *testing.T) {
	for _, k := range []string{"AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL", "AI_SEND_CODE"} {
		t.Setenv(k, "")
	}
	if _, err := Load(write(t, `{"ai":{"provider":"sim"}}`)); err != nil {
		t.Fatalf("default author email rejected: %v", err)
	}
	if _, err := Load(write(t, `{"ai":{"provider":"sim"},"github":{"commitAuthorEmail":"sarvas@users.noreply.github.com"}}`)); err == nil {
		t.Error("accepted a login-only noreply address")
	}
	if _, err := Load(write(t, `{"ai":{"provider":"sim"},"github":{"commitAuthorEmail":"41898282+my-app[bot]@users.noreply.github.com"}}`)); err != nil {
		t.Errorf("rejected an ID-prefixed bot address: %v", err)
	}
}

func TestSandboxBackendDefaultsToDocker(t *testing.T) {
	for _, k := range []string{"SANDBOX_BACKEND", "SANDBOX_MODAL_IMAGE", "SANDBOX_MAX_BUILDS_PER_USER_PER_DAY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
		"AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL", "AI_SEND_CODE"} {
		t.Setenv(k, "")
	}
	cfg, err := Load(write(t, `{"ai":{"provider":"sim"}}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Sandbox.Backend != "docker" || cfg.Sandbox.MaxBuildsPerUserPerDay != 10 || cfg.Sandbox.ModalImage == "" {
		t.Fatalf("%+v", cfg.Sandbox)
	}
	if cfg.Sandbox.TokenID != "" || cfg.Sandbox.TokenSecret != "" {
		t.Fatal("tokens must come from the environment, never the file")
	}
}

func TestAIAPIKeyEnvOverridesFileKey(t *testing.T) {
	for _, k := range []string{"AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL", "AI_SEND_CODE"} {
		t.Setenv(k, "")
	}
	t.Setenv("GEMINI_API_KEY", "gemini-key")
	t.Setenv("AI_PROVIDER", "openai")
	t.Setenv("AI_BASE_URL", "https://inference.baseten.co/v1")
	t.Setenv("AI_MODEL", "m")
	t.Setenv("AI_API_KEY", "baseten-key")
	cfg, err := Load(write(t, `{"ai":{"provider":"gemini","apiKey":"${GEMINI_API_KEY}"}}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.AI.APIKey != "baseten-key" {
		t.Fatalf("the file's Gemini key would be sent to %s", cfg.AI.BaseURL)
	}
}

func TestSandboxBackendFromEnv(t *testing.T) {
	for _, k := range []string{"AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL", "AI_SEND_CODE"} {
		t.Setenv(k, "")
	}
	t.Setenv("SANDBOX_BACKEND", "modal")
	t.Setenv("SANDBOX_MODAL_IMAGE", "my-image")
	t.Setenv("SANDBOX_MAX_BUILDS_PER_USER_PER_DAY", "3")
	t.Setenv("MODAL_TOKEN_ID", "tid")
	t.Setenv("MODAL_TOKEN_SECRET", "ts")
	cfg, err := Load(write(t, `{"ai":{"provider":"sim"}}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Sandbox.Backend != "modal" || cfg.Sandbox.ModalImage != "my-image" ||
		cfg.Sandbox.MaxBuildsPerUserPerDay != 3 || cfg.Sandbox.TokenID != "tid" || cfg.Sandbox.TokenSecret != "ts" {
		t.Fatalf("%+v", cfg.Sandbox)
	}
}

func TestSandboxBackendValidation(t *testing.T) {
	for _, k := range []string{"SANDBOX_BACKEND", "SANDBOX_MODAL_IMAGE", "SANDBOX_MAX_BUILDS_PER_USER_PER_DAY",
		"AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL", "AI_SEND_CODE"} {
		t.Setenv(k, "")
	}
	for _, body := range []string{
		`{"ai":{"provider":"sim"},"sandbox":{"backend":"lxc"}}`,
		`{"ai":{"provider":"sim"},"sandbox":{"backend":"modal","modalImage":""}}`,
		`{"ai":{"provider":"sim"},"sandbox":{"maxBuildsPerUserPerDay":-1}}`,
	} {
		if _, err := Load(write(t, body)); err == nil {
			t.Errorf("accepted %s", body)
		}
	}
}
