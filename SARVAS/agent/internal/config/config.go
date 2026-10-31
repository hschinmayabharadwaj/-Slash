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
	"strconv"
	"strings"
)

type AI struct {
	Provider string `json:"provider"` // "gemini" | "openai" (any OpenAI-compatible API) | "ollama" (self-hosted) | "sim" (offline)
	APIKey   string `json:"apiKey"`
	Model    string `json:"model"`
	BaseURL  string `json:"baseUrl"` // ollama (http://127.0.0.1:11434) or openai (https://api.fireworks.ai/inference/v1)
	// SendCode authorizes sending repository source code (Ask context, Build
	// tasks) to a REMOTE provider. Self-hosted providers never need it.
	SendCode bool `json:"sendCodeToProvider"`
}

// Remote reports whether prompts leave the operator's infrastructure.
func (a AI) Remote() bool { return a.Provider == "gemini" || a.Provider == "openai" }

// CodeAllowed reports whether repository source code may be sent to the provider.
func (a AI) CodeAllowed() bool { return !a.Remote() || a.SendCode }

type GitHub struct {
	ClientID       string `json:"clientId"`
	ClientSecret   string `json:"clientSecret"`
	AppID          string `json:"appId"`
	PrivateKeyPath string `json:"privateKeyPath"`
	InstallationID string `json:"installationId"`
	AuthorName     string `json:"commitAuthorName"`
	AuthorEmail    string `json:"commitAuthorEmail"`
}

// StringList is a JSON array of strings, or one comma-separated string (so
// it can come from an environment variable such as "${SARVAS_ALLOWED_REPOS}").
type StringList []string

func (l *StringList) UnmarshalJSON(data []byte) error {
	var list []string
	if err := json.Unmarshal(data, &list); err == nil {
		*l = list
		return nil
	}
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		return errors.New("expected a list of strings or a comma-separated string")
	}
	*l = nil
	for _, part := range strings.Split(s, ",") {
		if part = strings.TrimSpace(part); part != "" {
			*l = append(*l, part)
		}
	}
	return nil
}

type Security struct {
	AllowedRepos        StringList `json:"allowedRepos"`
	ProtectedPaths      StringList `json:"protectedPaths"`
	MaxFiles            int        `json:"maxFiles"`
	MaxChangedLines     int        `json:"maxChangedLines"`
	MaxFileSizeMB       int        `json:"maxFileSizeMb"`
	MaxRequestChars     int        `json:"maxRequestChars"`
	AgentNetwork        string     `json:"agentNetwork"` // network for the agent container
	ChecksNetwork       string     `json:"checksNetwork"`
	ScanSecrets         bool       `json:"scanSecrets"`
	BlockOnFailedChecks bool       `json:"blockOnFailedChecks"`
	KillSwitchFile      string     `json:"killSwitchFile"`
	MaxReadmeChars      int        `json:"maxReadmeChars"`
	// RequireProtectedBranch refuses to open a PR against a base branch that
	// GitHub doesn't protect with a pull-request rule (so a human must review).
	RequireProtectedBranch bool `json:"requireProtectedBranch"`
}

type Agent struct {
	MaxTurns             int `json:"maxTurns"`
	PlanningBudget       int `json:"planningBudget"`
	ImplementationBudget int `json:"implementationBudget"`
	PlanTimeoutS         int `json:"planTimeoutS"`
	ImplementTimeoutS    int `json:"implementTimeoutS"`
	CheckTimeoutS        int `json:"checkTimeoutS"`
	MaxConcurrentTasks   int `json:"maxConcurrentTasks"`
}

type Docker struct {
	Image       string `json:"image"`
	MemoryLimit string `json:"memoryLimit"`
	CPULimit    string `json:"cpuLimit"`
	PidsLimit   int    `json:"pidsLimit"`
}

// Sandbox selects which backend runs Build tasks. "docker" is the default;
// "modal" runs them in Modal Sandboxes (for hosts without Docker, such as
// Hugging Face Spaces or Render). Modal tokens come from the environment
// (MODAL_TOKEN_ID / MODAL_TOKEN_SECRET) only, never from the config file.
type Sandbox struct {
	Backend string `json:"backend"` // "docker" | "modal"
	// ModalImage is the named Modal image (published once from
	// sandbox/Dockerfile, never built per task) used by the modal backend.
	ModalImage string `json:"modalImage"`
	// MaxBuildsPerUserPerDay caps Build tasks per requester per rolling 24h
	// (0 = unlimited). Modal and hosted AI APIs bill per use.
	MaxBuildsPerUserPerDay int    `json:"maxBuildsPerUserPerDay"`
	TokenID                string `json:"-"`
	TokenSecret            string `json:"-"`
}

type Service struct {
	Addr    string `json:"addr"`    // agent-service listen address (loopback)
	Token   string `json:"token"`   // shared bearer token with the web server
	WorkDir string `json:"workDir"` // task checkouts (deleted after each phase)
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
	Sandbox      Sandbox         `json:"sandbox"`
	AgentService Service         `json:"agentService"`
	Repos        map[string]Repo `json:"repos"`
}

// Default returns the built-in defaults (secure by default).
func Default() Config {
	return Config{
		AI: AI{Provider: "gemini", Model: "gemini-3.8-flash"},
		GitHub: GitHub{
			AuthorName: "sarvas[bot]",
			// .invalid is reserved (RFC 2606), so GitHub can never link these
			// commits to someone's account.
			AuthorEmail: "agent@sarvas.invalid",
		},
		Security: Security{
			ProtectedPaths: []string{
				".git/**", ".github/workflows/**", ".env*", "**/secrets/**", "**/*.pem", "**/*.key",
			},
			MaxFiles: 20, MaxChangedLines: 500, MaxFileSizeMB: 10, MaxRequestChars: 2000,
			AgentNetwork: "bridge", ChecksNetwork: "none", ScanSecrets: true,
			BlockOnFailedChecks: true, KillSwitchFile: "./data/KILL_SWITCH", MaxReadmeChars: 50000,
			RequireProtectedBranch: true,
		},
		Agent: Agent{
			MaxTurns: 15, PlanningBudget: 20000, ImplementationBudget: 80000,
			PlanTimeoutS: 300, ImplementTimeoutS: 900, CheckTimeoutS: 600, MaxConcurrentTasks: 2,
		},
		Docker:       Docker{Image: "sarvas-sandbox:latest", MemoryLimit: "1g", CPULimit: "1.0", PidsLimit: 128},
		Sandbox:      Sandbox{Backend: "docker", ModalImage: "sarvas-sandbox", MaxBuildsPerUserPerDay: 10},
		AgentService: Service{Addr: "127.0.0.1:8766", WorkDir: "./data/work"},
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
	// AI_API_KEY wins like the other AI_* overrides, so switching AI_PROVIDER
	// never sends the file's key (e.g. ${GEMINI_API_KEY}) to another provider.
	if v := os.Getenv("AI_API_KEY"); v != "" {
		cfg.AI.APIKey = v
	}
	if cfg.AI.APIKey == "" {
		cfg.AI.APIKey = os.Getenv("GEMINI_API_KEY")
	}
	if v := os.Getenv("AI_BASE_URL"); v != "" {
		cfg.AI.BaseURL = v
	}
	if v := os.Getenv("OLLAMA_URL"); v != "" && cfg.AI.BaseURL == "" {
		cfg.AI.BaseURL = v
	}
	if v := os.Getenv("AI_MODEL"); v != "" {
		cfg.AI.Model = v
	}
	if v := os.Getenv("AI_SEND_CODE"); v != "" {
		cfg.AI.SendCode = v == "true" || v == "1"
	}
	if cfg.AI.Provider == "gemini" && cfg.AI.Model == "" {
		cfg.AI.Model = "gemini-3.8-flash"
	}
	if cfg.AI.Provider == "ollama" && cfg.AI.BaseURL == "" {
		cfg.AI.BaseURL = "http://127.0.0.1:11434"
	}
	if cfg.GitHub.ClientID == "" {
		cfg.GitHub.ClientID = os.Getenv("GITHUB_CLIENT_ID")
	}
	if cfg.GitHub.ClientSecret == "" {
		cfg.GitHub.ClientSecret = os.Getenv("GITHUB_CLIENT_SECRET")
	}
	if cfg.AgentService.Token == "" {
		cfg.AgentService.Token = os.Getenv("AGENT_SERVICE_TOKEN")
	}
	if v := os.Getenv("SANDBOX_BACKEND"); v != "" {
		cfg.Sandbox.Backend = v
	}
	if v := os.Getenv("SANDBOX_MODAL_IMAGE"); v != "" {
		cfg.Sandbox.ModalImage = v
	}
	if v := os.Getenv("SANDBOX_MAX_BUILDS_PER_USER_PER_DAY"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return cfg, fmt.Errorf("SANDBOX_MAX_BUILDS_PER_USER_PER_DAY: %w", err)
		}
		cfg.Sandbox.MaxBuildsPerUserPerDay = n
	}
	// Modal tokens live in the environment only, so they can never be
	// committed in a config file or echoed by config dumps.
	cfg.Sandbox.TokenID = os.Getenv("MODAL_TOKEN_ID")
	cfg.Sandbox.TokenSecret = os.Getenv("MODAL_TOKEN_SECRET")
	// Relative paths are relative to the config file, not the working directory.
	dir := filepath.Dir(path)
	for _, p := range []*string{&cfg.Security.KillSwitchFile, &cfg.GitHub.PrivateKeyPath, &cfg.AgentService.WorkDir} {
		if *p != "" && !filepath.IsAbs(*p) {
			*p = filepath.Join(dir, *p)
		}
	}
	return cfg, cfg.Validate()
}

var idNoreply = regexp.MustCompile(`^[0-9]+\+[^@]+@users\.noreply\.github\.com$`)

func (c Config) Validate() error {
	switch c.AI.Provider {
	case "sim":
	case "gemini":
		if len(c.AI.APIKey) < 10 {
			return errors.New("ai.apiKey (GEMINI_API_KEY) is missing or looks invalid")
		}
	case "ollama":
		if c.AI.Model == "" || c.AI.Model == "gemini-3.8-flash" {
			return errors.New("ai.model must name an Ollama model (e.g. qwen2.5-coder:7b) when ai.provider is ollama")
		}
	case "openai":
		if !strings.HasPrefix(c.AI.BaseURL, "http") || c.AI.Model == "" || c.AI.Model == "gemini-3.8-flash" {
			return errors.New("ai.provider openai needs ai.baseUrl (AI_BASE_URL, e.g. https://api.fireworks.ai/inference/v1) and ai.model (AI_MODEL)")
		}
	default:
		return fmt.Errorf("ai.provider must be gemini, openai, ollama or sim, got %q", c.AI.Provider)
	}
	if c.Sandbox.Backend != "docker" && c.Sandbox.Backend != "modal" {
		return fmt.Errorf("sandbox.backend must be docker or modal, got %q", c.Sandbox.Backend)
	}
	if c.Sandbox.MaxBuildsPerUserPerDay < 0 {
		return errors.New("sandbox.maxBuildsPerUserPerDay must not be negative (0 = unlimited)")
	}
	if c.Sandbox.Backend == "modal" && c.Sandbox.ModalImage == "" {
		return errors.New("sandbox.modalImage must name the published Modal image when sandbox.backend is modal")
	}
	for name, v := range map[string]string{"agentNetwork": c.Security.AgentNetwork, "checksNetwork": c.Security.ChecksNetwork} {
		if v != "none" && v != "bridge" {
			return fmt.Errorf("security.%s must be none or bridge, got %q", name, v)
		}
	}
	// GitHub credits "<login>@users.noreply.github.com" to the account named
	// <login>, so only the ID-prefixed form ("<id>+<login>@…", e.g. the App
	// bot's own address) is unambiguous.
	if e := strings.ToLower(c.GitHub.AuthorEmail); strings.HasSuffix(e, "@users.noreply.github.com") &&
		!idNoreply.MatchString(e) {
		return fmt.Errorf("github.commitAuthorEmail %q would credit commits to whichever GitHub user owns that name; "+
			"use the App bot's \"<id>+<slug>[bot]@users.noreply.github.com\" address or a non-GitHub address", c.GitHub.AuthorEmail)
	}
	if c.Agent.PlanningBudget+c.Agent.ImplementationBudget > 1_000_000 {
		return errors.New("agent token budgets are unreasonably large")
	}
	return nil
}

// AppConfigured reports whether installation-level GitHub App access (needed
// to clone private repos and open PRs from Build tasks) is configured.
func (c Config) AppConfigured() bool {
	return c.GitHub.AppID != "" && c.GitHub.PrivateKeyPath != ""
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
