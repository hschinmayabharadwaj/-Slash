package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"sarvas/internal/config"
	"sarvas/internal/github"
	"sarvas/internal/sandbox"
)

var httpClient = &http.Client{Timeout: 20 * time.Second}

type checker struct{ failed, warned int }

func (c *checker) ok(format string, a ...any) { fmt.Printf("  ✅ "+format+"\n", a...) }
func (c *checker) warn(format string, a ...any) {
	c.warned++
	fmt.Printf("  ⚠️  "+format+"\n", a...)
}
func (c *checker) fail(format string, a ...any) { c.failed++; fmt.Printf("  ❌ "+format+"\n", a...) }

// runCheck validates configuration, connectivity and permissions. It prints
// what it finds (never secret values) and returns the process exit code.
func runCheck(cfg config.Config) int {
	c := &checker{}
	fmt.Println("SARVAS setup check")

	fmt.Println("\nAI provider")
	checkAI(c, cfg)

	fmt.Println("\nGitHub sign-in (OAuth client of the GitHub App)")
	checkOAuthClient(c, cfg)

	fmt.Println("\nGitHub App installation access (Build tasks)")
	checkApp(c, cfg)

	fmt.Println("\nSandbox (Build tasks)")
	if !cfg.AppConfigured() {
		c.warn("skipped: Build tasks are off until the GitHub App key is configured")
	} else if problems := sandbox.New(cfg).Preflight(); len(problems) > 0 {
		for _, p := range problems {
			c.fail("%s", p)
		}
	} else {
		c.ok("Docker is reachable and the sandbox image %s exists", cfg.Docker.Image)
	}
	if err := os.MkdirAll(cfg.AgentService.WorkDir, 0o700); err != nil {
		c.fail("work directory %s is not writable: %v", cfg.AgentService.WorkDir, err)
	} else if f, err := os.CreateTemp(cfg.AgentService.WorkDir, ".check-*"); err != nil {
		c.fail("work directory %s is not writable: %v", cfg.AgentService.WorkDir, err)
	} else {
		f.Close()
		os.Remove(f.Name())
		c.ok("work directory %s is writable", cfg.AgentService.WorkDir)
	}

	fmt.Printf("\n%d problem(s), %d warning(s)\n", c.failed, c.warned)
	if c.failed > 0 {
		return 1
	}
	return 0
}

func checkAI(c *checker, cfg config.Config) {
	ai := cfg.AI
	switch ai.Provider {
	case "sim":
		c.warn("ai.provider is sim: answers and changes are canned placeholders, nothing is sent anywhere")
	case "ollama":
		res, err := httpClient.Get(strings.TrimRight(ai.BaseURL, "/") + "/api/tags")
		if err != nil {
			c.fail("can't reach Ollama at %s", ai.BaseURL)
			return
		}
		defer res.Body.Close()
		var tags struct {
			Models []struct {
				Name string `json:"name"`
			} `json:"models"`
		}
		json.NewDecoder(res.Body).Decode(&tags)
		for _, m := range tags.Models {
			if m.Name == ai.Model || m.Name == ai.Model+":latest" {
				c.ok("Ollama at %s has model %s (code stays on your infrastructure)", ai.BaseURL, ai.Model)
				return
			}
		}
		c.fail("Ollama is reachable but model %s isn't pulled; run: ollama pull %s", ai.Model, ai.Model)
	case "gemini":
		req, _ := http.NewRequest("GET", "https://generativelanguage.googleapis.com/v1beta/models/"+url.PathEscape(ai.Model), nil)
		req.Header.Set("x-goog-api-key", ai.APIKey)
		res, err := httpClient.Do(req)
		if err != nil {
			c.fail("can't reach the Gemini API")
			return
		}
		res.Body.Close()
		if res.StatusCode != 200 {
			c.fail("Gemini rejected the key or model %s (HTTP %d)", ai.Model, res.StatusCode)
			return
		}
		c.ok("Gemini key works with model %s", ai.Model)
		if ai.SendCode {
			c.warn("ai.sendCodeToProvider is on: repository excerpts and Build tasks are sent to Google")
		} else {
			c.warn("ai.sendCodeToProvider is off: Ask and Build are disabled for this remote provider (README drafting still works)")
		}
	}
}

func checkOAuthClient(c *checker, cfg config.Config) {
	gh := cfg.GitHub
	if gh.ClientID == "" || gh.ClientSecret == "" {
		c.fail("GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET are not set")
		return
	}
	// Checking a deliberately invalid token: GitHub answers 404 for valid
	// client credentials and 401 for invalid ones. No real token is involved.
	body, _ := json.Marshal(map[string]string{"access_token": "sarvas-setup-check"})
	req, _ := http.NewRequest("POST", "https://api.github.com/applications/"+url.PathEscape(gh.ClientID)+"/token", bytes.NewReader(body))
	req.Header.Set("Authorization", "Basic "+base64.StdEncoding.EncodeToString([]byte(gh.ClientID+":"+gh.ClientSecret)))
	req.Header.Set("Accept", "application/vnd.github+json")
	res, err := httpClient.Do(req)
	if err != nil {
		c.fail("can't reach api.github.com")
		return
	}
	res.Body.Close()
	switch res.StatusCode {
	case 404, 422:
		c.ok("client ID and secret are valid")
	case 401:
		c.fail("GitHub rejected the client ID / secret")
	default:
		c.warn("unexpected answer from GitHub (HTTP %d)", res.StatusCode)
	}
}

func checkApp(c *checker, cfg config.Config) {
	if !cfg.AppConfigured() {
		c.warn("github.appId / github.privateKeyPath not set: Ask and README work, Build tasks are off")
		return
	}
	if info, err := os.Stat(cfg.GitHub.PrivateKeyPath); err != nil {
		c.fail("private key %s not found", filepath.Base(cfg.GitHub.PrivateKeyPath))
		return
	} else if info.Mode().Perm()&0o077 != 0 {
		c.warn("private key %s is readable by other users; chmod 600 it", filepath.Base(cfg.GitHub.PrivateKeyPath))
	}
	app := &github.App{AppID: cfg.GitHub.AppID, PrivateKeyPath: cfg.GitHub.PrivateKeyPath}
	info, err := app.Check()
	if err != nil {
		c.fail("GitHub App %s: %v", cfg.GitHub.AppID, err)
		return
	}
	c.ok("GitHub App %q authenticated", info.Slug)
	if len(info.Installations) == 0 {
		c.fail("the App isn't installed anywhere; install it on the repositories SARVAS may work on")
	}
	need := map[string]string{"contents": "write", "pull_requests": "write", "metadata": "read"}
	for _, inst := range info.Installations {
		var missing []string
		for perm, level := range need {
			got := inst.Permissions[perm]
			if got != level && !(level == "read" && got == "write") {
				missing = append(missing, perm+":"+level)
			}
		}
		for perm, level := range inst.Permissions {
			if level == "admin" || perm == "administration" || perm == "workflows" {
				c.warn("installation on %s grants %s:%s — more than SARVAS needs", inst.Account, perm, level)
			}
		}
		if len(missing) > 0 {
			c.fail("installation on %s is missing %s", inst.Account, strings.Join(missing, ", "))
		} else {
			c.ok("installed on %s with the needed permissions", inst.Account)
		}
	}
}
