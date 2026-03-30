## For DNSMadeEasy: 

- swap out the caddy section of the compose to:
```yaml
caddy:
    build: 
      context: .
      dockerfile: Dockerfile.dmecaddy
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
    environment:
      - DOMAIN=${DOMAIN:-localhost}
      - ACME_EMAIL=${ACME_EMAIL:-}
      - DNSMADEEASY_API_KEY=${DNSMADEEASY_API_KEY}
      - DNSMADEEASY_SECRET_KEY=${DNSMADEEASY_SECRET_KEY}
    depends_on:
      - web
```
- Update Caddyfile

```caddyfile
{
    email {$ACME_EMAIL}
    acme_dns dnsmadeeasy {
        api_key {$DNSMADEEASY_API_KEY}
        secret_key {$DNSMADEEASY_SECRET_KEY}
    }
}

rmm.{$DOMAIN} {
    reverse_proxy web:8000
}
```

- Update .env 
inc