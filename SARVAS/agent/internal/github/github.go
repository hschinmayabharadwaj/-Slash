// Package github authenticates as a GitHub App installation (stdlib only).
//
// Installation tokens are requested per repository with only contents +
// pull-requests write permission, are never embedded in clone URLs (so they
// never land in .git/config, which the sandbox can read), and reach git only
// through GIT_CONFIG_* environment variables.
//
// Every REST call goes through an allowlist. There is deliberately no way to
// merge a pull request, submit a review, or change branch protection: the
// agent proposes, engineers approve and merge on GitHub.
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
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
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

// allowedCalls is every REST call this package may make. Merging, reviews,
// branch-protection changes and everything else are refused before any
// request is sent.
var allowedCalls = []struct {
	method string
	path   *regexp.Regexp
}{
	{"GET", regexp.MustCompile(`^/app$`)},
	{"GET", regexp.MustCompile(`^/app/installations$`)},
	{"GET", regexp.MustCompile(`^/repos/[^/]+/[^/]+/installation$`)},
	{"POST", regexp.MustCompile(`^/app/installations/\d+/access_tokens$`)},
	{"GET", regexp.MustCompile(`^/repos/[^/]+/[^/]+/branches/[^/]+$`)},
	{"GET", regexp.MustCompile(`^/repos/[^/]+/[^/]+/rules/branches/[^/]+$`)},
	{"POST", regexp.MustCompile(`^/repos/[^/]+/[^/]+/pulls$`)},
	{"POST", regexp.MustCompile(`^/repos/[^/]+/[^/]+/pulls/\d+/requested_reviewers$`)},
}

// ErrForbiddenCall is returned for any call outside allowedCalls.
var ErrForbiddenCall = errors.New("this GitHub API call is not allowed for the agent")

func callAllowed(method, path string) bool {
	if strings.Contains(path, "..") {
		return false
	}
	for _, c := range allowedCalls {
		if c.method == method && c.path.MatchString(path) {
			return true
		}
	}
	return false
}

// App is a GitHub App installation client.
type App struct {
	AppID          string
	PrivateKeyPath string
	InstallationID string // optional fallback; normally looked up per repository
	HTTP           *http.Client
	BaseURL        string // API base (tests)

	mu            sync.Mutex
	tokens        map[string]cachedToken
	installations map[string]string
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

// HTTPError is a non-2xx GitHub response (message only, never credentials).
type HTTPError struct {
	Status  int
	Message string
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("GitHub API error (HTTP %d): %s", e.Status, e.Message)
}

// call performs one allowlisted REST call with the given bearer credential.
func (a *App) call(method, path, bearer string, body, out any) error {
	if !callAllowed(method, path) {
		return fmt.Errorf("%w: %s %s", ErrForbiddenCall, method, path)
	}
	var reader io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		reader = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, a.base()+path, reader)
	if err == nil {
		req.URL.RawPath = path // keep %2F in branch names encoded
	}
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := a.client().Do(req)
	if err != nil {
		return errors.New("could not reach the GitHub API")
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if resp.StatusCode >= 300 {
		var e struct {
			Message string `json:"message"`
		}
		_ = json.Unmarshal(data, &e)
		if len(e.Message) > 200 {
			e.Message = e.Message[:200]
		}
		return &HTTPError{resp.StatusCode, e.Message}
	}
	if out != nil && len(data) > 0 {
		return json.Unmarshal(data, out)
	}
	return nil
}

func repoKey(owner, name string) string { return strings.ToLower(owner + "/" + name) }

// installation finds the App installation that covers owner/name.
func (a *App) installation(owner, name string) (string, error) {
	key := repoKey(owner, name)
	a.mu.Lock()
	id, ok := a.installations[key]
	a.mu.Unlock()
	if ok {
		return id, nil
	}
	jwt, err := a.JWT()
	if err != nil {
		return "", err
	}
	var out struct {
		ID int64 `json:"id"`
	}
	if err := a.call("GET", fmt.Sprintf("/repos/%s/%s/installation", owner, name), jwt, nil, &out); err != nil || out.ID == 0 {
		if a.InstallationID != "" {
			return a.InstallationID, nil
		}
		return "", fmt.Errorf("the GitHub App is not installed on %s/%s", owner, name)
	}
	id = strconv.FormatInt(out.ID, 10)
	a.mu.Lock()
	if a.installations == nil {
		a.installations = map[string]string{}
	}
	a.installations[key] = id
	a.mu.Unlock()
	return id, nil
}

// RepoToken returns an installation token scoped to ONE repository with only
// contents + pull-requests write permission (cached ~55 min).
func (a *App) RepoToken(owner, name string) (string, error) {
	if err := ValidateRepoName(owner, name); err != nil {
		return "", err
	}
	key := repoKey(owner, name)
	a.mu.Lock()
	t, ok := a.tokens[key]
	a.mu.Unlock()
	if ok && time.Now().Before(t.expires) {
		return t.value, nil
	}
	inst, err := a.installation(owner, name)
	if err != nil {
		return "", err
	}
	jwt, err := a.JWT()
	if err != nil {
		return "", err
	}
	var out struct {
		Token string `json:"token"`
	}
	err = a.call("POST", fmt.Sprintf("/app/installations/%s/access_tokens", inst), jwt, map[string]any{
		"repositories": []string{name},
		"permissions":  map[string]string{"contents": "write", "pull_requests": "write"},
	}, &out)
	if err != nil {
		return "", fmt.Errorf("installation token request failed: %w", err)
	}
	if out.Token == "" {
		return "", errors.New("installation token response invalid")
	}
	a.mu.Lock()
	if a.tokens == nil {
		a.tokens = map[string]cachedToken{}
	}
	a.tokens[key] = cachedToken{out.Token, time.Now().Add(55 * time.Minute)}
	a.mu.Unlock()
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
	tok, err := a.RepoToken(owner, name)
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

// CreatePR opens a pull request (never merges) and returns its URL.
func (a *App) CreatePR(owner, name, head, base, title, body string) (string, error) {
	tok, err := a.RepoToken(owner, name)
	if err != nil {
		return "", err
	}
	var out struct {
		HTMLURL string `json:"html_url"`
	}
	err = a.call("POST", fmt.Sprintf("/repos/%s/%s/pulls", owner, name), tok,
		map[string]any{"title": title, "head": head, "base": base, "body": body, "maintainer_can_modify": true}, &out)
	if err != nil {
		return "", fmt.Errorf("create pull request failed: %w", err)
	}
	return out.HTMLURL, nil
}

var prNumber = regexp.MustCompile(`/pull/(\d+)$`)

// RequestReviewers asks engineers to review the PR (best effort; GitHub only
// accepts collaborators, and never the PR author).
func (a *App) RequestReviewers(owner, name, prURL string, reviewers []string) error {
	m := prNumber.FindStringSubmatch(prURL)
	if m == nil || len(reviewers) == 0 {
		return nil
	}
	tok, err := a.RepoToken(owner, name)
	if err != nil {
		return err
	}
	return a.call("POST", fmt.Sprintf("/repos/%s/%s/pulls/%s/requested_reviewers", owner, name, m[1]), tok,
		map[string]any{"reviewers": reviewers}, nil)
}

// BranchProtected reports whether GitHub requires a pull request (classic
// branch protection or a ruleset with a pull_request rule) on branch.
func (a *App) BranchProtected(owner, name, branch string) (bool, error) {
	if err := ValidateRepoName(owner, name); err != nil {
		return false, err
	}
	tok, err := a.RepoToken(owner, name)
	if err != nil {
		return false, err
	}
	var rules []struct {
		Type string `json:"type"`
	}
	if err := a.call("GET", fmt.Sprintf("/repos/%s/%s/rules/branches/%s", owner, name, url.PathEscape(branch)), tok, nil, &rules); err == nil {
		for _, r := range rules {
			if r.Type == "pull_request" {
				return true, nil
			}
		}
	}
	var b struct {
		Protected bool `json:"protected"`
	}
	if err := a.call("GET", fmt.Sprintf("/repos/%s/%s/branches/%s", owner, name, url.PathEscape(branch)), tok, nil, &b); err != nil {
		return false, err
	}
	return b.Protected, nil
}

// AppInfo is what Check reports about the App and its installations.
type AppInfo struct {
	Slug          string
	Installations []Installation
}

type Installation struct {
	Account     string
	Permissions map[string]string
}

// Check verifies the App ID + private key with GitHub and lists installations.
func (a *App) Check() (AppInfo, error) {
	var info AppInfo
	jwt, err := a.JWT()
	if err != nil {
		return info, err
	}
	var app struct {
		Slug string `json:"slug"`
	}
	if err := a.call("GET", "/app", jwt, nil, &app); err != nil {
		return info, err
	}
	info.Slug = app.Slug
	var inst []struct {
		Account struct {
			Login string `json:"login"`
		} `json:"account"`
		Permissions map[string]string `json:"permissions"`
	}
	if err := a.call("GET", "/app/installations", jwt, nil, &inst); err != nil {
		return info, err
	}
	for _, i := range inst {
		info.Installations = append(info.Installations, Installation{i.Account.Login, i.Permissions})
	}
	return info, nil
}
