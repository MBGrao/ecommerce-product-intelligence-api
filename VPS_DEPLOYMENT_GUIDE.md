# 🚀 VPS Deployment Guide for Product Intelligence API

Complete step-by-step guide to deploy the Product Intelligence API on your VPS. Replace `<your-vps-ip>` with your server's IP address throughout this guide.

## 📋 Prerequisites

- **VPS**: AlmaLinux 8+ or RHEL 8+
- **Root Access**: SSH access to your VPS
- **Domain**: Optional (for SSL certificates)

## 🔧 Step 1: Initial VPS Setup

### SSH to your VPS
```bash
ssh root@<your-vps-ip>
```

### Update system packages
```bash
dnf update -y
dnf install -y epel-release
```

### Install essential packages
```bash
dnf install -y python3.11 python3.11-pip nodejs npm git curl wget unzip
```

### Verify installations
```bash
python3.11 --version  # Should show Python 3.11.x
node --version         # Should show v18.x.x
npm --version          # Should show 9.x.x
```

## 🐍 Step 2: Python Environment Setup

### Create application directory
```bash
mkdir -p /opt/product-analyzer
cd /opt/product-analyzer
```

### Clone the repository
```bash
git clone https://github.com/MBGrao/ecommerce-product-intelligence-api.git .
```

### Create virtual environment
```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

### Install Python dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

## 🤖 Step 3: Playwright Setup

### Install Playwright browsers
```bash
# Make sure you're in the virtual environment
source .venv/bin/activate
playwright install chromium
```

### Verify Playwright installation
```bash
playwright --version
```

## ⚙️ Step 4: Environment Configuration

### Create environment file

Copy the template and fill in your own values:

```bash
cp .env.example /opt/product-analyzer/.env

# Generate a strong API key
openssl rand -hex 32
```

Edit `/opt/product-analyzer/.env` and set:

- `API_KEY` — the value you just generated (clients send it as the `X-API-Key` header)
- `GOOGLE_API_KEY` — your Google Cloud Vision API key

> ⚠️ Never commit the `.env` file or paste real keys into documentation.
> If a key is ever exposed, rotate it immediately.

## 🚀 Step 5: Systemd Service Setup

### Copy service file
```bash
cp clean_service_file.service /etc/systemd/system/product-analyzer.service
```

The service loads its configuration from `/opt/product-analyzer/.env`
(created in Step 4) via `EnvironmentFile`.

### Reload systemd
```bash
systemctl daemon-reload
```

### Enable and start service
```bash
systemctl enable product-analyzer
systemctl start product-analyzer
```

### Check service status
```bash
systemctl status product-analyzer
```

## 🌐 Step 6: Reverse Proxy Setup (Caddy)

### Install Docker (if not already installed)
```bash
dnf install -y docker
systemctl enable docker
systemctl start docker
```

### Create Caddyfile
```bash
cat > /tmp/Caddyfile << 'EOF'
api.<your-vps-ip>.sslip.io {
    reverse_proxy 172.17.0.1:8000
}
EOF
```

### Run Caddy container
```bash
docker run -d --name caddy --restart unless-stopped \
  -p 80:80 -p 443:443 \
  -v /tmp/Caddyfile:/etc/caddy/Caddyfile \
  caddy:2
```

## ✅ Step 7: Verification

### Test local API
```bash
curl -s http://localhost:8000/health
```

### Test through reverse proxy
```bash
curl -s https://api.<your-vps-ip>.sslip.io/health
```

### Test AliExpress scraping
```bash
curl -X POST "https://api.<your-vps-ip>.sslip.io/analyze/full" \
  -H "X-API-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"https://httpbin.org/image/png","url":"https://ar.aliexpress.com/item/1005006158280083.html","language":"ar"}'
```

## 🔍 Step 8: Monitoring and Logs

### View service logs
```bash
journalctl -u product-analyzer -f
```

### Check service status
```bash
systemctl status product-analyzer
```

### Monitor port usage
```bash
netstat -tlnp | grep :8000
```

### Check Docker containers
```bash
docker ps
docker logs caddy
```

## 🚨 Troubleshooting

### Service won't start
```bash
# Check logs
journalctl -u product-analyzer -n 50

# Check environment
systemctl show product-analyzer --property=Environment

# Restart service
systemctl restart product-analyzer
```

### Playwright issues
```bash
# Reinstall Playwright
source /opt/product-analyzer/.venv/bin/activate
playwright install --force chromium

# Check Node.js path
which node
echo $PATH
```

### Caddy issues
```bash
# Restart Caddy
docker restart caddy

# Check Caddy logs
docker logs caddy

# Verify configuration
docker exec caddy cat /etc/caddy/Caddyfile
```

### Port conflicts
```bash
# Check what's using ports 80/443
netstat -tlnp | grep -E ':(80|443)'

# Kill conflicting processes
fuser -k 80/tcp
fuser -k 443/tcp
```

## 🔄 Step 9: Updates and Maintenance

### Update code
```bash
cd /opt/product-analyzer
git pull origin main
```

### Restart service after updates
```bash
systemctl restart product-analyzer
```

### Update dependencies
```bash
source .venv/bin/activate
pip install -r requirements.txt --upgrade
```

## 📊 Performance Monitoring

### Check memory usage
```bash
free -h
ps aux | grep python
```

### Check disk space
```bash
df -h
du -sh /opt/product-analyzer
```

### Monitor API performance
```bash
# Test response time
time curl -s "https://api.<your-vps-ip>.sslip.io/health"
```

## 🔐 Security Considerations

### Firewall setup
```bash
# Allow only necessary ports
firewall-cmd --permanent --add-service=ssh
firewall-cmd --permanent --add-service=http
firewall-cmd --permanent --add-service=https
firewall-cmd --reload
```

### Regular updates
```bash
# Update system packages
dnf update -y

# Update Python packages
source /opt/product-analyzer/.venv/bin/activate
pip list --outdated
pip install -r requirements.txt --upgrade
```

## 📝 Final Notes

- **API Key**: Keep your API key secure and rotate it regularly
- **Backups**: Consider backing up your configuration files
- **Monitoring**: Set up monitoring for uptime and performance
- **Updates**: Keep the system and dependencies updated

## 🆘 Support

If you encounter issues:
1. Check the logs: `journalctl -u product-analyzer -f`
2. Verify service status: `systemctl status product-analyzer`
3. Test local connectivity: `curl http://localhost:8000/health`
4. Check reverse proxy: `docker logs caddy`

---

**Deployment completed successfully!** 🎉

Your Product Intelligence API is now running at:
- **Local**: http://localhost:8000
- **Public**: https://api.<your-vps-ip>.sslip.io
