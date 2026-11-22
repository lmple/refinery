# Refinery quality gate. `make gate` is what CI and agents run; the individual
# targets exist so a single failing check can be re-run quickly.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := gate

JAVA ?= java
UV   ?= uv

# Pinned to the latest *stable* release; the checksum makes a tampered or
# silently re-uploaded jar fail the build instead of running.
TLA_VERSION := 1.7.4
TLA_SHA256  := 936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
TLA_URL     := https://github.com/tlaplus/tlaplus/releases/download/v$(TLA_VERSION)/tla2tools.jar
TLA_JAR     := tools/tla2tools.jar

# TLC writes its state queue to ./states by default; keep it out of the tree.
TLC_METADIR := .tlc

SPECS     := $(wildcard spec/*.tla)
BIG_CFGS  := $(wildcard spec/*_big.cfg)

# A module without a .cfg is only allowed if it opts out explicitly; otherwise
# a forgotten .cfg would silently drop a spec from model checking.
HELPER_MARK := \* refinery: helper

.PHONY: gate tools tla tla-big test types lint

gate: test types lint tla

tools: $(TLA_JAR)

# Download to a temp name and only move into place once the checksum matches,
# so an interrupted or bad download never leaves a jar that make considers fresh.
$(TLA_JAR):
	mkdir -p $(@D)
	curl -fsSL -o $@.part $(TLA_URL)
	echo "$(TLA_SHA256)  $@.part" | sha256sum --check --quiet
	mv $@.part $@

# SANY parses every module; TLC runs every module against its <M>.cfg.
# A module with no .cfg fails the gate unless it carries the helper marker.
# Never pass -deadlock here: terminal states must stutter explicitly instead.
tla: $(TLA_JAR)
	@if [ -z "$(SPECS)" ]; then echo "tla: no spec/*.tla files, nothing to check"; exit 0; fi
	@for tla in $(SPECS); do \
	  mod=$$(basename "$$tla" .tla); \
	  cfg="spec/$$mod.cfg"; \
	  echo "== SANY $$tla"; \
	  $(JAVA) -cp $(TLA_JAR) tla2sany.SANY "$$tla"; \
	  if [ -f "$$cfg" ]; then \
	    echo "== TLC  $$tla ($$cfg)"; \
	    $(JAVA) -jar $(TLA_JAR) -workers auto -metadir "$(TLC_METADIR)/$$mod" \
	      -config "$$cfg" "$$tla"; \
	  elif grep -qF '$(HELPER_MARK)' "$$tla"; then \
	    echo "== TLC  $$tla skipped: helper module"; \
	  else \
	    echo "tla: $$tla has no $$cfg and is not marked '$(HELPER_MARK)'" >&2; \
	    exit 1; \
	  fi; \
	done

# Occasional deep checks: TLC on each spec/<M>_big.cfg against spec/<M>.tla.
# Not part of the gate because these are allowed to run for a long time.
tla-big: $(TLA_JAR)
	@if [ -z "$(BIG_CFGS)" ]; then echo "tla-big: no spec/*_big.cfg files, nothing to check"; exit 0; fi
	@for cfg in $(BIG_CFGS); do \
	  mod=$$(basename "$$cfg" _big.cfg); \
	  tla="spec/$$mod.tla"; \
	  if [ ! -f "$$tla" ]; then echo "tla-big: $$cfg has no matching $$tla" >&2; exit 1; fi; \
	  echo "== SANY $$tla"; \
	  $(JAVA) -cp $(TLA_JAR) tla2sany.SANY "$$tla"; \
	  echo "== TLC  $$tla ($$cfg)"; \
	  $(JAVA) -jar $(TLA_JAR) -workers auto -metadir "$(TLC_METADIR)/$${mod}_big" \
	    -config "$$cfg" "$$tla"; \
	done

test:
	$(UV) run pytest -q

types:
	$(UV) run pyright

lint:
	$(UV) run ruff check
	$(UV) run ruff format --check
