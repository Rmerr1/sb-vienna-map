#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Subway Builder map toolchain installer (Ubuntu / WSL)
#
# Installs everything `depot` needs on PATH:
#   node, mapshaper, osmium, java, tippecanoe, tile-join, sqlite3, jq,
#   pmtiles, planetiler.jar   (+ Miniforge for the Python environment)
#
# Docker is NOT installed here - on Windows you want Docker Desktop with WSL
# integration enabled. See the README.
#
# Usage:   bash 00_install_toolchain.sh
# Re-runnable: every step is skipped if the tool is already present.
# ---------------------------------------------------------------------------
set -euo pipefail

TOOLS_DIR="${TOOLS_DIR:-$HOME/sb-tools}"
mkdir -p "$TOOLS_DIR/bin"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- 1. apt packages -------------------------------------------------------
say "Installing apt packages (you'll be asked for your sudo password)"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    build-essential git curl ca-certificates unzip \
    sqlite3 libsqlite3-dev zlib1g-dev \
    jq osmium-tool osmctools gdal-bin \
    openjdk-21-jre-headless \
    python3-venv

# --- 2. Node (via nvm, avoids the ancient apt version) ---------------------
if ! have node; then
    say "Installing Node via nvm"
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
    export NVM_DIR="$HOME/.nvm"
    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"
    nvm install --lts
else
    say "Node already present: $(node --version)"
fi

# --- 3. mapshaper ----------------------------------------------------------
if ! have mapshaper; then
    say "Installing mapshaper"
    npm install -g mapshaper
else
    say "mapshaper already present: $(mapshaper --version 2>/dev/null || echo ok)"
fi

# --- 4. tippecanoe + tile-join (built from source) -------------------------
if ! have tippecanoe; then
    say "Building tippecanoe from source (takes a few minutes)"
    tmp="$(mktemp -d)"
    git clone --depth 1 https://github.com/felt/tippecanoe.git "$tmp/tippecanoe"
    make -C "$tmp/tippecanoe" -j"$(nproc)"
    sudo make -C "$tmp/tippecanoe" install
    rm -rf "$tmp"
else
    say "tippecanoe already present: $(tippecanoe --version 2>&1 | head -1)"
fi

# --- 5. pmtiles CLI (go-pmtiles release binary) ----------------------------
if ! have pmtiles; then
    say "Installing pmtiles CLI"
    PM_TAG="$(curl -fsSL https://api.github.com/repos/protomaps/go-pmtiles/releases/latest | jq -r .tag_name)"
    PM_VER="${PM_TAG#v}"
    curl -fsSL -o /tmp/pmtiles.tgz \
        "https://github.com/protomaps/go-pmtiles/releases/download/${PM_TAG}/go-pmtiles_${PM_VER}_Linux_x86_64.tar.gz"
    tar -xzf /tmp/pmtiles.tgz -C "$TOOLS_DIR/bin" pmtiles
    chmod +x "$TOOLS_DIR/bin/pmtiles"
    rm -f /tmp/pmtiles.tgz
else
    say "pmtiles already present"
fi

# --- 6. planetiler.jar -----------------------------------------------------
if [ ! -f "$TOOLS_DIR/planetiler.jar" ]; then
    say "Downloading planetiler.jar"
    curl -fsSL -o "$TOOLS_DIR/planetiler.jar" \
        https://github.com/onthegomap/planetiler/releases/latest/download/planetiler.jar
else
    say "planetiler.jar already present"
fi
# depot looks for planetiler.jar on PATH as a file, so expose it there too
ln -sf "$TOOLS_DIR/planetiler.jar" "$TOOLS_DIR/bin/planetiler.jar"

# --- 7. Miniforge (conda) --------------------------------------------------
if ! have conda; then
    say "Installing Miniforge (conda)"
    curl -fsSL -o /tmp/miniforge.sh \
        "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
    bash /tmp/miniforge.sh -b -p "$HOME/miniforge3"
    "$HOME/miniforge3/bin/conda" init bash
    rm -f /tmp/miniforge.sh
else
    say "conda already present"
fi

# --- 8. PATH ---------------------------------------------------------------
if ! grep -q 'sb-tools/bin' "$HOME/.bashrc" 2>/dev/null; then
    say "Adding $TOOLS_DIR/bin to PATH in ~/.bashrc"
    printf '\n# Subway Builder map tools\nexport PATH="%s/bin:$PATH"\n' "$TOOLS_DIR" >> "$HOME/.bashrc"
fi
export PATH="$TOOLS_DIR/bin:$PATH"

# --- 9. Verify -------------------------------------------------------------
say "Verifying toolchain"
missing=0
for t in node mapshaper osmium java tippecanoe tile-join sqlite3 jq pmtiles; do
    if have "$t"; then
        printf '  \033[0;32mOK\033[0m      %s\n' "$t"
    else
        printf '  \033[0;31mMISSING\033[0m %s\n' "$t"; missing=1
    fi
done
[ -f "$TOOLS_DIR/planetiler.jar" ] \
    && printf '  \033[0;32mOK\033[0m      planetiler.jar\n' \
    || { printf '  \033[0;31mMISSING\033[0m planetiler.jar\n'; missing=1; }
have docker \
    && printf '  \033[0;32mOK\033[0m      docker (needed for OSRM routing)\n' \
    || printf '  \033[0;33mTODO\033[0m    docker - install Docker Desktop and enable WSL integration\n'

echo
if [ "$missing" -eq 0 ]; then
    say "Toolchain complete. Now run:  source ~/.bashrc && conda env create -f environment.yml"
else
    say "Some tools are missing - see above before continuing."
    exit 1
fi
