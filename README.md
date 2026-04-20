#  Docksmith — A Minimal Docker-like System

Docksmith is a simplified container build and runtime system inspired by Docker.  
It supports building images from a `Docksmithfile`, caching layers deterministically, and running containers in an isolated filesystem.

---

## Project Structure

    docksmith/
    │── main.py
    │── storage.py
    │── build_engine.py
    │── cache.py
    │── runtime.py
    │── stubs.py (dev only)
    │── sampleapp/
    │ ├── Docksmithfile
    │ ├── app.sh
    │── alpine.tar

---

## Features

### Build System
- Parses `Docksmithfile`
- Supports instructions:
  - `FROM`
  - `WORKDIR`
  - `COPY`
  - `RUN`
  - `ENV`
  - `CMD`

---

### Layered Filesystem
- Each `COPY` and `RUN` creates a **layer**
- Layers stored in:

        ~/.docksmith/layers/

- Layers are:
  - Immutable
  - Content-addressed (SHA256)

---

### Deterministic Build Cache

Cache key is computed using:

previous layer digest +
instruction text +
WORKDIR +
sorted ENV +
(COPY → file hashes)


- Cache Hit → layer reused
- Cache Miss → layer rebuilt
- Miss cascades to all subsequent steps

---

### Runtime System
- Assembles filesystem by extracting layers
- Runs process inside isolated root filesystem
- Uses Linux primitives (`chroot`, namespaces)

---

### Isolation Guarantee
> Files created inside the container do NOT appear on the host system

---

## Setup Instructions

### 1. Install Linux / WSL (Required for Runtime)

```bash
wsl --install
```

### 2. Import Base Image (One-time Setup)

```bash
docker pull alpine:3.18
docker save alpine:3.18 -o alpine.tar

python3 main.py import-image alpine.tar alpine:3.18
```

## Usage

### Build Image

    python3 main.py build -t sample:v1 sampleapp

### Rebuild (Cache Demonstration)

    python3 main.py build -t sample:v1 sampleapp

### Expected output:

    [CACHE HIT]

### Run Container
    sudo -E python3 main.py run sample:v1

### Example output:

    Hello from docksmith-sample!
    Running in offline-only container — no network required.

### Verify Isolation
    
    ls /app/secret.txt

### Output:

    No such file or directory

### Rerun the project

    python3 main.py rmi sample:v1
    python3 main.py import-image alpine.tar alpine:3.18

### Show images 

    python3 main.py images

---