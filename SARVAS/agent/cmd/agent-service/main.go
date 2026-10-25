// agent-service: the localhost-only AI/guard service used by the web server.
//
//	agent-service -config ../config.json
package main

import (
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"sarvas/internal/config"
	"sarvas/internal/service"
)

func main() {
	path := flag.String("config", "config.json", "path to config.json")
	check := flag.Bool("check", false, "validate configuration, connectivity and permissions, then exit")
	flag.Parse()
	cfg, err := config.Load(*path)
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	if *check {
		os.Exit(runCheck(cfg))
	}
	if len(cfg.AgentService.Token) < 32 {
		log.Fatal("agentService.token (AGENT_SERVICE_TOKEN) must be set to a random value of at least 32 characters")
	}
	host, _, err := net.SplitHostPort(cfg.AgentService.Addr)
	if err != nil {
		log.Fatalf("agentService.addr: %v", err)
	}
	if ip := net.ParseIP(host); host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		log.Printf("WARNING: agentService.addr %s is not loopback; it should only be reachable by the web server", cfg.AgentService.Addr)
	}
	svc := service.New(cfg)
	build := "on"
	if problems := svc.BuildProblems(); len(problems) > 0 {
		build = "off (" + strings.Join(problems, "; ") + ")"
	}
	srv := &http.Server{
		Addr:              cfg.AgentService.Addr,
		Handler:           svc.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      150 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}
	log.Printf("agent-service listening on %s (ai=%s, build tasks %s)", cfg.AgentService.Addr, cfg.AI.Provider, build)
	log.Fatal(srv.ListenAndServe())
}
