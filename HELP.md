# Custom Help Information

This file is optional. Add any custom instructions, host-specific info, or team guidelines here.

## Host Information

### Production Servers
- **web01.example.com** - Frontend web server (nginx)
- **web02.example.com** - Frontend web server (nginx)
- **db01.example.com** - Primary database (PostgreSQL)

### Important Notes
- All hosts use the `monitor` user for SSH access
- Disk alerts trigger at 80% usage
- Memory alerts trigger at 90% usage

## Common Issues

### High Disk Usage
1. Check disk space: `@bob check disk on <host>`
2. Look for large log files in `/var/log`
3. Check for old backups in `/backup`

### High Memory
1. Check memory: `@bob check memory on <host>`
2. Check top processes: `@bob check processes on <host>`
3. Consider restarting services if needed

## Team Contacts
- On-call: Check #oncall channel
- Database team: @db-team
- Network team: @network-team

---
*Edit this file to add your own custom help information*
