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
	t.Setenv("GEMINI_API_KEY", "")
	for _, body := range []string{
		`{"ai":{"provider":"gemini"}}`,
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
