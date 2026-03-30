# Use an official lightweight Python image
FROM python:3.11-slim
FROM caddy:2-builder AS builder

# Set the working directory inside the container
WORKDIR /app


# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare


# Copy the rest of the application code
COPY . .

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy

# Expose port 8000 for the web server
EXPOSE 8000

# Command to run the application using Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]