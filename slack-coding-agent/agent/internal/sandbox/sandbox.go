// Package sandbox runs one throwaway Docker container per agent run or check.
//
//   - read-only root filesystem, size-limited tmpfs, all capabilities dropped,
//     no-new-privileges, pid/memory/CPU limits, hard wall-clock timeout;
//   - runs as the host user; /workspace/.git is mounted read-only;
//   - no GitHub credentials ever enter a container; the agent container only
//     gets the AI key (via the environment, never argv) and network only
//     when the provider needs it; checks always run with network "none".
package sandbox

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"time"

	"codingagent/internal/config"
	"codingagent/internal/guards"
)

type Result struct {
	ExitCode int
	TimedOut bool
	Stdout   string
	Stderr   string
}

// Runner is what the pipeline needs (fakes implement it in tests).
type Runner interface {
	RunAgent(mode, repoDir, ioDir string, timeout time.Duration, apiKey string) (Result, error)
	RunChecks(repoDir, command string, timeout time.Duration) (Result, error)
}

type Docker struct {
	Cfg           config.Docker
	AgentNetwork  string
	ChecksNetwork string
	Provider      string
	// Exec is swappable for tests.
	Exec func(ctx context.Context, name string, args []string, env []string) (Result, error)
}

func New(cfg config.Config) *Docker {
	agentNet := cfg.Security.AgentNetwork
	if cfg.AI.Provider == "sim" {
		agentNet = "none" // the offline provider needs no network at all
	}
	return &Docker{Cfg: cfg.Docker, AgentNetwork: agentNet, ChecksNetwork: cfg.Security.ChecksNetwork, Provider: cfg.AI.Provider}
}

func (d *Docker) baseArgs(name, network, repoDir string, rw bool) []string {
	repo, _ := filepath.Abs(repoDir)
	mode := "ro"
	if rw {
		mode = "rw"
	}
	args := []string{
		"run", "--rm", "--name", name,
		"--read-only",
		"--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
		"--cap-drop", "ALL",
		"--security-opt", "no-new-privileges",
		"--pids-limit", strconv.Itoa(d.Cfg.PidsLimit),
		"--memory", d.Cfg.MemoryLimit,
		"--memory-swap", d.Cfg.MemoryLimit,
		"--cpus", d.Cfg.CPULimit,
		"--network", network,
		"--user", fmt.Sprintf("%d:%d", os.Getuid(), os.Getgid()),
		"-e", "HOME=/tmp/home",
		"-v", repo + ":/workspace:" + mode,
	}
	if _, err := os.Stat(filepath.Join(repo, ".git")); err == nil {
		args = append(args, "-v", filepath.Join(repo, ".git")+":/workspace/.git:ro")
	}
	return append(args, "-w", "/workspace")
}

func containerName(kind string) string {
	return fmt.Sprintf("coding-agent-%s-%d", kind, time.Now().UnixNano())
}

// RunAgent runs agent-runner (mode = plan | implement). Plan mounts read-only.
func (d *Docker) RunAgent(mode, repoDir, ioDir string, timeout time.Duration, apiKey string) (Result, error) {
	if mode != "plan" && mode != "implement" {
		return Result{}, fmt.Errorf("unknown agent mode %q", mode)
	}
	name := containerName(mode)
	args := d.baseArgs(name, d.AgentNetwork, repoDir, mode == "implement")
	io, _ := filepath.Abs(ioDir)
	args = append(args, "-v", io+":/io:rw", "-e", "AI_PROVIDER="+d.Provider)
	var env []string
	if apiKey != "" {
		// "-e NAME" with no value copies it from docker's own environment,
		// keeping the secret out of the process list.
		args = append(args, "-e", "GEMINI_API_KEY")
		env = append(env, "GEMINI_API_KEY="+apiKey)
	}
	args = append(args, d.Cfg.Image, "/usr/local/bin/agent-runner",
		"--mode", mode, "--input", "/io/input.json", "--output", "/io/output.json")
	return d.run(name, args, env, timeout)
}

// RunChecks runs a trusted command from config (never model-written), network off.
func (d *Docker) RunChecks(repoDir, command string, timeout time.Duration) (Result, error) {
	name := containerName("checks")
	args := append(d.baseArgs(name, d.ChecksNetwork, repoDir, true), d.Cfg.Image, "sh", "-c", command)
	return d.run(name, args, nil, timeout)
}

func (d *Docker) run(name string, args, env []string, timeout time.Duration) (Result, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	execFn := d.Exec
	if execFn == nil {
		execFn = dockerExec
	}
	res, err := execFn(ctx, name, args, env)
	if ctx.Err() == context.DeadlineExceeded {
		_ = exec.Command("docker", "kill", name).Run()
		res.TimedOut, res.ExitCode = true, -9
		return res, nil
	}
	res.Stdout, res.Stderr = guards.Tail(res.Stdout, 8000), guards.Tail(res.Stderr, 8000)
	return res, err
}

func dockerExec(ctx context.Context, _ string, args, env []string) (Result, error) {
	cmd := exec.CommandContext(ctx, "docker", args...)
	cmd.Env = append(os.Environ(), env...)
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	err := cmd.Run()
	res := Result{Stdout: out.String(), Stderr: errb.String()}
	if exitErr, ok := err.(*exec.ExitError); ok {
		res.ExitCode = exitErr.ExitCode()
		err = nil
	}
	return res, err
}

// Preflight returns problems (empty = Docker and the image are ready).
func (d *Docker) Preflight() []string {
	if err := exec.Command("docker", "info").Run(); err != nil {
		return []string{"Docker is not available or not running"}
	}
	if err := exec.Command("docker", "image", "inspect", d.Cfg.Image).Run(); err != nil {
		return []string{fmt.Sprintf("sandbox image %q not found — build it with: docker build -t %s .", d.Cfg.Image, d.Cfg.Image)}
	}
	return nil
}
