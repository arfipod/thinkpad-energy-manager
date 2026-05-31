#!/usr/bin/env bash
set -euo pipefail

thinkpad-energy-manager collect --mode blackbox --name "blackbox-$(date +%Y%m%d-%H%M%S)"
