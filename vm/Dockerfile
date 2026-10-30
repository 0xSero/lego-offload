# The VM an agent runs in: Linux, the agents, and nothing of the Mac. lego mounts only the workspace
# and a throwaway home, at the same paths as on the Mac.
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git ripgrep python3 curl unzip procps \
    && rm -rf /var/lib/apt/lists/*
ARG CLAUDE=2.1.314
ARG OMP=18.13.3
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE} bun && npm cache clean --force
ENV BUN_INSTALL=/opt/bun PATH=/opt/bun/bin:$PATH
RUN bun install -g @oh-my-pi/pi-coding-agent@${OMP} && omp --version && claude --version
