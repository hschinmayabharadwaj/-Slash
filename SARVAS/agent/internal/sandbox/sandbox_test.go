package sandbox

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"sarvas/internal/config"
)

type call struct{ args, env []string }

func capture(d *Docker) *[]call {
	var calls []call
	d.Exec = func(_ context.Context, _ string, args, env []string) (Result, error) {
		calls = append(calls, call{args, env})
		return Result{}, nil
	}
	return &calls
}

func after(args []string, flag string) []string {
	var out []string
	for i, a := range args {
		if a == flag && i+1 < len(args) {
			out = append(out, args[i+1])
		}
	}
	return out
}

func TestAPIKeyNeverInArgv(t *testing.T) {
	cfg := config.Default()
	d := New(cfg)
	calls := capture(d)
	d.RunAgent("plan", t.TempDir(), t.TempDir(), time.Second, "AIzaSECRET")
	c := (*calls)[0]
	if strings.Contains(strings.Join(c.args, " "), "AIzaSECRET") {
		t.Fatal("key in argv")
	}
	if strings.Join(c.env, ",") != "GEMINI_API_KEY=AIzaSECRET" {
		t.Fatalf("env %v", c.env)
	}
}

func TestHardeningAndReadOnlyPlan(t *testing.T) {
	repo := t.TempDir()
	os.Mkdir(filepath.Join(repo, ".git"), 0o755)
	d := New(config.Default())
	calls := capture(d)
	d.RunAgent("plan", repo, t.TempDir(), time.Second, "")
	args := (*calls)[0].args
	for _, f := range []string{"--read-only", "--cap-drop", "--security-opt", "--pids-limit", "--memory"} {
		if len(after(args, f)) == 0 && !contains(args, f) {
			t.Errorf("missing %s", f)
		}
	}
	mounts := strings.Join(after(args, "-v"), " ")
	if !strings.Contains(mounts, ":/workspace:ro") || !strings.Contains(mounts, "/.git:/workspace/.git:ro") {
		t.Fatalf("mounts %s", mounts)
	}
}

func TestNetworkPolicy(t *testing.T) {
	cfg := config.Default()
	d := New(cfg)
	calls := capture(d)
	d.RunChecks(t.TempDir(), "pytest", time.Second)
	if after((*calls)[0].args, "--network")[0] != "none" {
		t.Fatal("checks must be offline")
	}
	cfg.AI.Provider = "sim"
	d = New(cfg)
	calls = capture(d)
	d.RunAgent("implement", t.TempDir(), t.TempDir(), time.Second, "")
	if after((*calls)[0].args, "--network")[0] != "none" {
		t.Fatal("sim agent must be offline")
	}
}

func TestUnknownMode(t *testing.T) {
	if _, err := New(config.Default()).RunAgent("rm", ".", ".", time.Second, ""); err == nil {
		t.Fatal("expected error")
	}
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
