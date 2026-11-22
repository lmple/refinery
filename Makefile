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

# SANY + TLC (+ reachability witnesses) on every spec/*.tla, and the
# occasional *_big.cfg deep checks; scripts/tla.sh documents the rules.
tla: $(TLA_JAR)
	JAVA=$(JAVA) TLA_JAR=$(TLA_JAR) scripts/tla.sh gate

tla-big: $(TLA_JAR)
	JAVA=$(JAVA) TLA_JAR=$(TLA_JAR) scripts/tla.sh big

test:
	$(UV) run pytest -q

types:
	$(UV) run pyright

lint:
	$(UV) run ruff check
	$(UV) run ruff format --check
