# Use Python 3.11 slim image for smaller size
FROM python:3.11-slim

# Install system dependencies for Chrome, browser automation, and git (needed for py2js)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    xvfb \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user for security
RUN useradd -m -u 1000 mcpuser && chown -R mcpuser:mcpuser /app
USER mcpuser

# Expose port (Smithery will set PORT env var)
EXPOSE 8000
ENV PORT=8000

# Health check for FastMCP HTTP server
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD if [ -n "$STEALTH_BROWSER_MCP_AUTH_TOKEN" ]; then \
        curl -fsS -H "Authorization: Bearer $STEALTH_BROWSER_MCP_AUTH_TOKEN" http://localhost:$PORT/mcp -o /dev/null || exit 1; \
    else \
        curl -fsS http://localhost:$PORT/mcp -o /dev/null || exit 1; \
    fi

# Start HTTP transport. Set STEALTH_BROWSER_MCP_AUTH_TOKEN to enable bearer auth.
CMD ["python", "src/server.py", "--transport", "http", "--host", "0.0.0.0"]
