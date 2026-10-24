// Package config loads the shared config.json (also read by the FastAPI web
// server). String values may reference environment variables as ${NAME}.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

type AI struct {
	Provider string `json:"provider"` // "gemini" | "sim"
	APIKey   string `json:"apiKey"`
	Model    string `json:"model"`
}

type GitHub struct {
	AppID          string `json:"appId"`
	PrivateKeyPath string `json:"privateKeyPath"`
	InstallationID string `json:"installationId"`
	AuthorName     string `json:"commitAuthorName"`
	AuthorEmail    string `json:"commitAuthorEmail"`
}

type Security struct {
	AllowedRepos        []string `json:"allowedRepos"`
	ProtectedPaths      []string `json:"protectedPaths"`
	MaxFiles            int      `json:"maxFiles"`
	MaxChangedLines     int      `json:"maxChangedLines"`
	MaxFileSizeMB       int      `json:"maxFileSizeMb"`
	MaxRequestChars     int      `json:"maxRequestChars"`
	AgentNetwork        string   `json:"agentNetwork"` // network for the agent container
	ChecksNetwork       string   `json:"checksNetwork"`
	ScanSecrets         bool     `json:"scanSecrets"`
	BlockOnFailedChecks bool     `json:"blockOnFailedChecks"`
	KillSwitchFile      string   `json:"killSwitchFile"`
	MaxReadmeChars      int      `json:"maxReadmeChars"`
}

type Agent struct {
	MaxTurns             int `json:"maxTurns"`
	PlanningBudget       int `json:"planningBudget"`
	ImplementationBudget int `json:"implementationBudget"`
	PlanTimeoutS         int `json:"planTimeoutS"`
	ImplementTimeoutS    int `json:"implementTimeoutS"`
	CheckTimeoutS        int `json:"checkTimeoutS"`
}

type Docker struct {
	Image       string `json:"image"`
	MemoryLimit string `json:"memoryLimit"`
	CPULimit    string `json:"cpuLimit"`
	PidsLimit   int    `json:"pidsLimit"`
}

type Service struct {
	Addr  string `json:"addr"`  // agent-service listen address (loopback)
	Token string `json:"token"` // shared bearer token with the web server
}

type Repo struct {
	DefaultBranch  string   `json:"defaultBranch"`
	LintCommand    string   `json:"lintCommand"`
	TestCommand    string   `json:"testCommand"`
	ExtraDenyPaths []string `json:"extraDenyPaths"`
	Enabled        *bool    `json:"enabled"`
	MaxFiles       int      `json:"maxFiles"`
	MaxLines       int      `json:"maxLines"`
}

type Config struct {
	AI           AI              `json:"ai"`
	GitHub       GitHub          `json:"github"`
	Security     Security        `json:"security"`
	Agent        Agent           `json:"agent"`
	Docker       Docker          `json:"docker"`
	AgentService Service         `json:"agentService"`
	Repos        map[string]Repo `json:"repos"`
}

// Default returns the built-in defaults (secure by default).
func Default() Config {
	return Config{
		AI: AI{Provider: "gemini", Model: "gemini-2.5-flash"},
		GitHub: GitHub{
			AuthorName:  "sarvas[bot]",
			AuthorEmail: "sarvas@users.noreply.github.com",
		},
		Security: Security{
			ProtectedPaths: []string{
				".git/**", ".github/workflows/**", ".env*", "**/secrets/**", "**/*.pem", "**/*.key",
			},
			MaxFiles: 20, MaxChangedLines: 500, MaxFileSizeMB: 10, MaxRequestChars: 2000,
			AgentNetwork: "bridge", ChecksNetwork: "none", ScanSecrets: true,
			BlockOnFailedChecks: true, KillSwitchFile: "./data/KILL_SWITCH", MaxReadmeChars: 50000,
		},
		Agent: Agent{
			MaxTurns: 15, PlanningBudget: 20000, ImplementationBudget: 80000,
			PlanTimeoutS: 300, ImplementTimeoutS: 900, CheckTimeoutS: 600,
		},
		Docker:       Docker{Image: "sarvas-sandbox:latest", MemoryLimit: "1g", CPULimit: "1.0", PidsLimit: 128},
		AgentService: Service{Addr: "127.0.0.1:8766"},
		Repos:        map[string]Repo{},
	}
}

var envRef = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)

// expand replaces ${VAR} in every JSON string value.
func expand(v any) any {
	switch t := v.(type) {
	case string:
		return envRef.ReplaceAllStringFunc(t, func(m string) string {
			return os.Getenv(envRef.FindStringSubmatch(m)[1])
		})
	case map[string]any:
		for k, val := range t {
			t[k] = expand(val)
		}
	case []any:
		for i, val := range t {
			t[i] = expand(val)
		}
	}
	return v
}

// Load reads path (missing file = defaults) and validates the result.
func Load(path string) (Config, error) {
	cfg := Default()
	data, err := os.ReadFile(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return cfg, err
	}
	if err == nil {
		var raw any
		if err := json.Unmarshal(data, &raw); err != nil {
			return cfg, fmt.Errorf("%s: %w", path, err)
		}
		expanded, _ := json.Marshal(expand(raw))
		if err := json.Unmarshal(expanded, &cfg); err != nil {
			return cfg, fmt.Errorf("%s: %w", path, err)
		}
	}
	if p := os.Getenv("AI_PROVIDER"); p != "" {
		cfg.AI.Provider = p
	}
	if cfg.AI.APIKey == "" {
		cfg.AI.APIKey = os.Getenv("GEMINI_API_KEY")
	}
	if cfg.AgentService.Token == "" {
		cfg.AgentService.Token = os.Getenv("AGENT_SERVICE_TOKEN")
	}
	// Relative paths are relative to the config file, not the working directory.
	dir := filepath.Dir(path)
	for _, p := range []*string{&cfg.Security.KillSwitchFile, &cfg.GitHub.PrivateKeyPath} {
		if *p != "" && !filepath.IsAbs(*p) {
			*p = filepath.Join(dir, *p)
		}
	}
	return cfg, cfg.Validate()
}

func (c Config) Validate() error {
	switch c.AI.Provider {
	case "sim":
	case "gemini":
		if len(c.AI.APIKey) < 10 {
			return errors.New("ai.apiKey (GEMINI_API_KEY) is missing or looks invalid")
		}
	default:
		return fmt.Errorf("ai.provider must be gemini or sim, got %q", c.AI.Provider)
	}
	for name, v := range map[string]string{"agentNetwork": c.Security.AgentNetwork, "checksNetwork": c.Security.ChecksNetwork} {
		if v != "none" && v != "bridge" {
			return fmt.Errorf("security.%s must be none or bridge, got %q", name, v)
		}
	}
	if c.Agent.PlanningBudget+c.Agent.ImplementationBudget > 1_000_000 {
		return errors.New("agent token budgets are unreasonably large")
	}
	return nil
}

// Repo returns per-repo settings with defaults applied.
func (c Config) Repo(fullName string) Repo {
	r := c.Repos[fullName]
	if r.DefaultBranch == "" {
		r.DefaultBranch = "main"
	}
	return r
}

// RepoAllowed reports whether the agent may work on owner/name.
func (c Config) RepoAllowed(fullName string) bool {
	if r, ok := c.Repos[fullName]; ok && r.Enabled != nil && !*r.Enabled {
		return false
	}
	if len(c.Security.AllowedRepos) == 0 {
		return true
	}
	for _, a := range c.Security.AllowedRepos {
		if strings.EqualFold(a, fullName) {
			return true
		}
	}
	return false
}

// DenyFor returns global protected paths plus the repo's extra deny paths.
func (c Config) DenyFor(fullName string) []string {
	return append(append([]string{}, c.Security.ProtectedPaths...), c.Repo(fullName).ExtraDenyPaths...)
}
