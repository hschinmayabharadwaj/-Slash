// Package github authenticates as a GitHub App installation (stdlib only).
//
// Installation tokens are requested per repository with only contents +
// pull-requests write permission, are never embedded in clone URLs (so they
// never land in .git/config, which the sandbox can read), and reach git only
// through GIT_CONFIG_* environment variables.
package github

import (
	"bytes"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"os"
	"regexp"
	"sync"
	"time"
)

const API = "https://api.github.com"

var nameRe = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}$`)

// ValidateRepoName rejects owner/repo values that could be abused in URLs or paths.
func ValidateRepoName(owner, name string) error {
	for _, p := range []string{owner, name} {
		if !nameRe.MatchString(p) || p == "." || p == ".." {
			return fmt.Errorf("invalid repository name: %s/%s", owner, name)
		}
	}
	return nil
}

// App is a GitHub App installation client.
type App struct {
	AppID          string
	PrivateKeyPath string
	InstallationID string
	HTTP           *http.Client
	BaseURL        string // API base (tests)

	mu     sync.Mutex
	tokens map[string]cachedToken
}

type cachedToken struct {
	value   string
	expires time.Time
}

func (a *App) client() *http.Client {
	if a.HTTP != nil {
		return a.HTTP
	}
	return &http.Client{Timeout: 30 * time.Second}
}

func (a *App) base() string {
	if a.BaseURL != "" {
		return a.BaseURL
	}
	return API
}

func b64(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

// JWT signs a short-lived RS256 app JWT.
func (a *App) JWT() (string, error) {
	data, err := os.ReadFile(a.PrivateKeyPath)
	if err != nil {
		return "", fmt.Errorf("read GitHub App key: %w", err)
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return "", errors.New("GitHub App key is not PEM")
	}
	var key *rsa.PrivateKey
	if k, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		key = k
	} else if k8, err := x509.ParsePKCS8PrivateKey(block.Bytes); err == nil {
		rk, ok := k8.(*rsa.PrivateKey)
		if !ok {
			return "", errors.New("GitHub App key is not RSA")
		}
		key = rk
	} else {
		return "", errors.New("cannot parse GitHub App key")
	}
	now := time.Now().Unix()
	header := b64([]byte(`{"alg":"RS256","typ":"JWT"}`))
	claims, _ := json.Marshal(map[string]any{"iat": now - 60, "exp": now + 9*60, "iss": a.AppID})
	signing := header + "." + b64(claims)
	digest := sha256.Sum256([]byte(signing))
	sig, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, digest[:])
	if err != nil {
		return "", err
	}
	return signing + "." + b64(sig), nil
}

// Token returns an installation token scoped to one repo (cached ~55 min).
func (a *App) Token(repo string) (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if t, ok := a.tokens[repo]; ok && time.Now().Before(t.expires) {
		return t.value, nil
	}
	jwt, err := a.JWT()
	if err != nil {
		return "", err
	}
	body, _ := json.Marshal(map[string]any{
		"repositories": []string{repo},
		"permissions":  map[string]string{"contents": "write", "pull_requests": "write"},
	})
	req, _ := http.NewRequest("POST", fmt.Sprintf("%s/app/installations/%s/access_tokens", a.base(), a.InstallationID), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+jwt)
	req.Header.Set("Accept", "application/vnd.github+json")
	resp, err := a.client().Do(req)
	if err != nil {
		return "", fmt.Errorf("installation token request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return "", fmt.Errorf("installation token request failed: HTTP %d", resp.StatusCode)
	}
	var out struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil || out.Token == "" {
		return "", errors.New("installation token response invalid")
	}
	if a.tokens == nil {
		a.tokens = map[string]cachedToken{}
	}
	a.tokens[repo] = cachedToken{out.Token, time.Now().Add(55 * time.Minute)}
	return out.Token, nil
}

// CloneURL is a credential-free HTTPS URL.
func (a *App) CloneURL(owner, name string) (string, error) {
	if err := ValidateRepoName(owner, name); err != nil {
		return "", err
	}
	return fmt.Sprintf("https://github.com/%s/%s.git", owner, name), nil
}

// AuthEnv authenticates a single git command via GIT_CONFIG_* (never argv, never disk).
func (a *App) AuthEnv(owner, name string) ([]string, error) {
	tok, err := a.Token(name)
	if err != nil {
		return nil, err
	}
	basic := base64.StdEncoding.EncodeToString([]byte("x-access-token:" + tok))
	return []string{
		"GIT_CONFIG_COUNT=1",
		"GIT_CONFIG_KEY_0=http.https://github.com/.extraheader",
		"GIT_CONFIG_VALUE_0=AUTHORIZATION: basic " + basic,
	}, nil
}

// CreatePR opens a pull request and returns its URL.
func (a *App) CreatePR(owner, name, head, base, title, body string) (string, error) {
	if err := ValidateRepoName(owner, name); err != nil {
		return "", err
	}
	tok, err := a.Token(name)
	if err != nil {
		return "", err
	}
	payload, _ := json.Marshal(map[string]string{"title": title, "head": head, "base": base, "body": body})
	req, _ := http.NewRequest("POST", fmt.Sprintf("%s/repos/%s/%s/pulls", a.base(), owner, name), bytes.NewReader(payload))
	req.Header.Set("Authorization", "Bearer "+tok)
	req.Header.Set("Accept", "application/vnd.github+json")
	resp, err := a.client().Do(req)
	if err != nil {
		return "", errors.New("create pull request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return "", fmt.Errorf("create pull request failed: HTTP %d", resp.StatusCode)
	}
	var out struct {
		HTMLURL string `json:"html_url"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return out.HTMLURL, nil
}
