# Raspberry Pi WiFi AP + Bridge Mode Complete Setup

This guide configures a Raspberry Pi as:

- WiFi Access Point (AP)
- DHCP server
- Internet gateway/router
- Local LAN provider
- Video server host

This configuration supports:

- Raspberry Pi camera nodes
- laptops
- mobile phones
- browser access to the video system

while also providing internet access through Ethernet uplink.

---

# Target Network

WiFi SSID:

```text
archeryNet
```

Password:

```text
archery2026
```

Internal LAN:

```text
192.168.60.0/24
```

Server address:

```text
192.168.60.1
```

---

# Recommended Hardware

Recommended:

- Raspberry Pi 5
- Raspberry Pi OS Lite 64-bit
- Active cooling
- Ethernet internet uplink

Architecture:

```text
Internet
    |
 Ethernet uplink
    |
+----------------------+
| Raspberry Pi Server  |
| WiFi AP + Router     |
| Video Server         |
+----------+-----------+
           )))
      archeryNet WiFi
           )))
     ___ ___|____ ___
    /       |        \
 node-01  laptop   mobile
 node-02
```

---

# 1. Update System

```bash
sudo apt update
sudo apt upgrade -y
```

Reboot if needed:

```bash
sudo reboot
```

---

# 2. Install Required Packages

```bash
sudo apt install -y \
    hostapd \
    dnsmasq \
    dhcpcd5 \
    iptables-persistent
```

---

# 3. Enable Services

```bash
sudo systemctl unmask hostapd
```

```bash
sudo systemctl enable hostapd
```

```bash
sudo systemctl enable dnsmasq
```

```bash
sudo systemctl enable dhcpcd
```

---

# 4. Disable NetworkManager Control Of wlan0

Modern Raspberry Pi OS versions may use NetworkManager.

This conflicts with hostapd AP mode.

Create:

```bash
sudo mkdir -p /etc/NetworkManager/conf.d
```

```bash
sudo nano /etc/NetworkManager/conf.d/unmanaged.conf
```

Contents:

```text
[keyfile]
unmanaged-devices=interface-name:wlan0
```

Restart NetworkManager:

```bash
sudo systemctl restart NetworkManager
```

---

# 5. Configure Static IP For wlan0

Edit:

```bash
sudo nano /etc/dhcpcd.conf
```

Add at end:

```text
interface wlan0
static ip_address=192.168.60.1/24
nohook wpa_supplicant
```

This gives the AP a fixed address.

---

# 6. Configure dnsmasq DHCP Server

Backup original config:

```bash
sudo mv /etc/dnsmasq.conf /etc/dnsmasq.conf.orig
```

Create new config:

```bash
sudo nano /etc/dnsmasq.conf
```

Contents:

```text
interface=wlan0

# DHCP range

dhcp-range=192.168.60.100,192.168.60.200,255.255.255.0,24h

# Gateway

dhcp-option=3,192.168.60.1

# DNS

dhcp-option=6,192.168.60.1
```

---

# 7. Configure hostapd

Create:

```bash
sudo nano /etc/hostapd/hostapd.conf
```

Contents:

```text
country_code=SE
interface=wlan0
ssid=archeryNet

# 5 GHz AP mode
hw_mode=a
channel=36

ieee80211n=1
ieee80211ac=1
wmm_enabled=1

macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0

# WPA2
wpa=2
wpa_passphrase=archery2026
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP

# Regulatory
ieee80211d=1
ieee80211h=1
```

---

# 8. Point hostapd To Config File

Edit:

```bash
sudo nano /etc/default/hostapd
```

Set:

```text
DAEMON_CONF="/etc/hostapd/hostapd.conf"
```

---

# 9. Enable IPv4 Forwarding

Edit:

```bash
sudo nano /etc/sysctl.conf
```

Find or add:

```text
net.ipv4.ip_forward=1
```

Apply immediately:

```bash
sudo sysctl -p
```

---

# 10. Configure NAT / Internet Sharing

Assumptions:

```text
eth0  = internet uplink
wlan0 = archeryNet AP
```

Add NAT rule:

```bash
sudo iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
```

Allow forwarding:

```bash
sudo iptables -A FORWARD -i eth0 -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT
```

```bash
sudo iptables -A FORWARD -i wlan0 -o eth0 -j ACCEPT
```

Save rules:

```bash
sudo netfilter-persistent save
```

---

# 11. Reboot

```bash
sudo reboot
```

---

# 12. Verify Configuration

## Check wlan0 IP

```bash
ip addr show wlan0
```

Expected:

```text
inet 192.168.60.1/24
```

IMPORTANT:

If this IP is missing:
- AP will not function
- DHCP will fail
- clients will not connect.

---

## Verify hostapd

```bash
sudo systemctl status hostapd
```

Expected:

```text
active (running)
```

---

## Verify dnsmasq

```bash
sudo systemctl status dnsmasq
```

Expected:

```text
active (running)
```

---

# 13. Verify WiFi Network

On phone/laptop:

Connect to:

```text
SSID: archeryNet
Password: archery2026
```

Verify:

- IP address assigned
- internet works
- browser opens:

```text
http://192.168.60.1/
```

---

# 14. Configure Nodes

On each node:

Edit:

```bash
sudo nano /etc/wpa_supplicant/wpa_supplicant.conf
```

Add:

```text
network={
    ssid="archeryNet"
    psk="archery2026"
}
```

Reboot:

```bash
sudo reboot
```

Verify:

```bash
hostname -I
```

Expected:

```text
192.168.60.xxx
```

---

# 15. Verify Node Connectivity

From server:

```bash
curl http://192.168.60.xxx:8080/health
```

Verify:

- nodes online
- trigger-all works
- uploads work
- browser playback works

---

# 16. Optional: Fixed DHCP Leases

Recommended for stable node addresses.

Edit:

```bash
sudo nano /etc/dnsmasq.conf
```

Add entries:

```text
dhcp-host=b8:27:eb:11:22:33,192.168.60.101

dhcp-host=b8:27:eb:44:55:66,192.168.60.102
```

Get MAC addresses:

```bash
ip link
```

Restart dnsmasq:

```bash
sudo systemctl restart dnsmasq
```

---

# 17. Useful Commands

## Show AP clients

```bash
iw dev wlan0 station dump
```

---

## Show DHCP leases

```bash
cat /var/lib/misc/dnsmasq.leases
```

---

## View AP logs

```bash
journalctl -u hostapd -f
```

---

## View DHCP logs

```bash
journalctl -u dnsmasq -f
```

---

## Check internet routing

```bash
ping 8.8.8.8
```

---

## Check server web UI

```text
http://192.168.60.1/
```

---

# Troubleshooting

## wlan0 has no IP

Check:

```bash
ip addr show wlan0
```

If no:

```text
inet 192.168.60.1/24
```

then:

- dhcpcd is not controlling wlan0
- NetworkManager conflict exists
- AP cannot function.

Verify:

```bash
systemctl status dhcpcd
```

Verify unmanaged config exists:

```bash
/etc/NetworkManager/conf.d/unmanaged.conf
```

---

## WiFi network not visible

Check:

```bash
sudo systemctl status hostapd
```

View logs:

```bash
journalctl -u hostapd -n 50
```

---

## Clients connect but no IP address

Check:

```bash
sudo systemctl status dnsmasq
```

View leases:

```bash
cat /var/lib/misc/dnsmasq.leases
```

---

## Clients have no internet

Check NAT rules:

```bash
sudo iptables -t nat -L
```

Verify forwarding enabled:

```bash
cat /proc/sys/net/ipv4/ip_forward
```

Should return:

```text
1
```

---

# Final Notes

This architecture provides:

- self-contained WiFi network
- local browser access
- mobile/laptop connectivity
- node auto-discovery
- internet access through server
- no external router dependency

The video system continues functioning locally even if internet disappears.

