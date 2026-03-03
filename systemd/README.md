# Systemd Services

Service files for running components as systemd services.

## Installation

1. Edit service files and replace `YOUR_USER` with your actual username:
   ```bash
   sed -i "s/YOUR_USER/$USER/g" *.service
   ```

2. Symlink to systemd directory (better - updates automatically):
   ```bash
   sudo ln -s /opt/infra-assistant/systemd/zabbix-proxy.service /etc/systemd/system/
   sudo ln -s /opt/infra-assistant/systemd/rc-bot.service /etc/systemd/system/
   ```

3. Reload systemd:
   ```bash
   sudo systemctl daemon-reload
   ```

4. Enable and start services:
   ```bash
   # Zabbix Proxy
   sudo systemctl enable zabbix-proxy
   sudo systemctl start zabbix-proxy

   # RC Bot
   sudo systemctl enable rc-bot
   sudo systemctl start rc-bot
   ```

5. Check status:
   ```bash
   sudo systemctl status zabbix-proxy
   sudo systemctl status rc-bot
   ```

6. View logs:
   ```bash
   tail -f /opt/infra-assistant/logs/zabbix-proxy.log
   tail -f /opt/infra-assistant/logs/rc-bot.log

   # Or with journalctl:
   journalctl -u zabbix-proxy -f
   journalctl -u rc-bot -f
   ```

## Services

| Service | Port | Description |
|---------|------|-------------|
| zabbix-proxy | 5002 | REST API for Zabbix |
| rc-bot | - | Rocket.Chat bridge |

## Managing Services

```bash
# Start
sudo systemctl start SERVICE_NAME

# Stop
sudo systemctl stop SERVICE_NAME

# Restart
sudo systemctl restart SERVICE_NAME

# Status
sudo systemctl status SERVICE_NAME

# Enable (auto-start on boot)
sudo systemctl enable SERVICE_NAME

# Disable
sudo systemctl disable SERVICE_NAME
```
