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
