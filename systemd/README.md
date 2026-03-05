# Systemd Services

Service files for running components as systemd services.

## Installation

1. Edit service files and replace `YOUR_USER` with your actual username:
   ```bash
   sed -i "s/YOUR_USER/$USER/g" *.service
   ```

2. Symlink to systemd directory (replace with your actual path):
   ```bash
   INSTALL_DIR="/data/local/infra-assistant"  # Or wherever you cloned it
   sudo ln -s ${INSTALL_DIR}/systemd/ssh-proxy.service /etc/systemd/system/
   sudo ln -s ${INSTALL_DIR}/systemd/zabbix-proxy.service /etc/systemd/system/
   sudo ln -s ${INSTALL_DIR}/systemd/rc-bot.service /etc/systemd/system/
   sudo ln -s ${INSTALL_DIR}/systemd/zabbix-poller.service /etc/systemd/system/
   sudo ln -s ${INSTALL_DIR}/systemd/zabbix-poller.timer /etc/systemd/system/
   ```

3. Reload systemd:
   ```bash
   sudo systemctl daemon-reload
   ```

4. Enable and start services:
   ```bash
   # SSH Proxy (independent command validator)
   sudo systemctl enable ssh-proxy
   sudo systemctl start ssh-proxy

   # Zabbix Proxy
   sudo systemctl enable zabbix-proxy
   sudo systemctl start zabbix-proxy

   # RC Bot
   sudo systemctl enable rc-bot
   sudo systemctl start rc-bot

   # Zabbix Poller (timer - runs every 5 minutes)
   sudo systemctl enable zabbix-poller.timer
   sudo systemctl start zabbix-poller.timer
   ```

5. Check status:
   ```bash
   sudo systemctl status ssh-proxy zabbix-proxy rc-bot
   ```

6. View logs:
   ```bash
   # With journalctl (recommended):
   journalctl -u ssh-proxy -f
   journalctl -u zabbix-proxy -f
   journalctl -u rc-bot -f
   ```

## Services

| Service | Port | Description |
|---------|------|-------------|
| ssh-proxy | 5001 | SSH command validator (independent) |
| zabbix-proxy | 5002 | REST API for Zabbix |
| rc-bot | - | Rocket.Chat bridge with Ollama |
| zabbix-poller | - | Periodic alert checker (timer) |

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
