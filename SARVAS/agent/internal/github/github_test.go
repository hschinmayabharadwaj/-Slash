package github

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestJWTAndScopedToken(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	keyPath := filepath.Join(t.TempDir(), "app.pem")
	os.WriteFile(keyPath, pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}), 0o600)

	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		jwt := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		parts := strings.Split(jwt, ".")
		sig, _ := base64.RawURLEncoding.DecodeString(parts[2])
		digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
		if rsa.VerifyPKCS1v15(&key.PublicKey, crypto.SHA256, digest[:], sig) != nil {
			w.WriteHeader(401)
			return
		}
		json.NewDecoder(r.Body).Decode(&body)
		w.Write([]byte(`{"token":"ghs_abcdefghijklmnopqrstu"}`))
	}))
	defer srv.Close()

	app := &App{AppID: "1", PrivateKeyPath: keyPath, InstallationID: "2", BaseURL: srv.URL}
	env, err := app.AuthEnv("org", "repo")
	if err != nil {
		t.Fatal(err)
	}
	repos, _ := body["repositories"].([]any)
	if len(repos) != 1 || repos[0] != "repo" {
		t.Fatalf("token not scoped to repo: %v", body)
	}
	if strings.Contains(strings.Join(env, " "), "ghs_") {
		t.Fatal("raw token in env value (should be base64 header)")
	}
	url, _ := app.CloneURL("org", "repo")
	if strings.Contains(url, "@") {
		t.Fatalf("credentials in clone URL: %s", url)
	}
}

func TestValidateRepoName(t *testing.T) {
	for _, bad := range [][2]string{{"..", "x"}, {"a b", "c"}, {"org", "repo;rm"}, {"", "x"}} {
		if ValidateRepoName(bad[0], bad[1]) == nil {
			t.Errorf("%v accepted", bad)
		}
	}
	if ValidateRepoName("octo", "my-repo.go") != nil {
		t.Error("valid name rejected")
	}
}

func testApp(t *testing.T, handler http.HandlerFunc) *App {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	keyPath := filepath.Join(t.TempDir(), "app.pem")
	os.WriteFile(keyPath, pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}), 0o600)
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	return &App{AppID: "1", PrivateKeyPath: keyPath, BaseURL: srv.URL}
}

func TestNoMergeOrReviewCalls(t *testing.T) {
	for _, c := range [][2]string{
		{"PUT", "/repos/o/r/pulls/1/merge"},
		{"POST", "/repos/o/r/pulls/1/reviews"},
		{"PUT", "/repos/o/r/branches/main/protection"},
		{"DELETE", "/repos/o/r/branches/main/protection"},
		{"PATCH", "/repos/o/r"},
		{"POST", "/repos/o/r/merges"},
		{"POST", "/repos/o/r/pulls/../merges"},
	} {
		if callAllowed(c[0], c[1]) {
			t.Errorf("%s %s must be refused", c[0], c[1])
		}
	}
	app := testApp(t, func(w http.ResponseWriter, r *http.Request) { t.Errorf("request sent: %s", r.URL) })
	if err := app.call("PUT", "/repos/o/r/pulls/1/merge", "x", nil, nil); !errors.Is(err, ErrForbiddenCall) {
		t.Fatalf("%v", err)
	}
}

func TestPerRepoInstallationAndProtection(t *testing.T) {
	var tokenReqs []string
	app := testApp(t, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/repos/acme/web/installation":
			w.Write([]byte(`{"id":77}`))
		case r.URL.Path == "/app/installations/77/access_tokens":
			tokenReqs = append(tokenReqs, r.URL.Path)
			w.Write([]byte(`{"token":"ghs_scopedtoken12345678"}`))
		case r.URL.EscapedPath() == "/repos/acme/web/rules/branches/release%2F1":
			w.Write([]byte(`[{"type":"pull_request"}]`))
		case r.URL.Path == "/repos/acme/web/rules/branches/main":
			w.Write([]byte(`[]`))
		case r.URL.Path == "/repos/acme/web/branches/main":
			w.Write([]byte(`{"name":"main","protected":false}`))
		default:
			w.WriteHeader(404)
		}
	})
	if ok, err := app.BranchProtected("acme", "web", "release/1"); err != nil || !ok {
		t.Fatalf("ruleset protection not detected: %v %v", ok, err)
	}
	if ok, err := app.BranchProtected("acme", "web", "main"); err != nil || ok {
		t.Fatalf("unprotected main reported protected: %v %v", ok, err)
	}
	if len(tokenReqs) != 1 {
		t.Fatalf("token should be cached per repo, got %d requests", len(tokenReqs))
	}
}

func TestNotInstalled(t *testing.T) {
	app := testApp(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(404) })
	if _, err := app.RepoToken("acme", "other"); err == nil || !strings.Contains(err.Error(), "not installed") {
		t.Fatalf("%v", err)
	}
}
