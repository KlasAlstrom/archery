# archery
Video system for archery training

Test cam check for H264
v4l2-ctl --list-formats-ext

Start node
python3 main.py

Trigg manually
curl -X POST http://raspberrypi.local:8080/trigger

Check health
curl http://raspberrypi.local:8080/health

Install system dependencies
sudo apt update
sudo apt install -y \
    python3-pip \
    python3-venv \
    ffmpeg \
    v4l-utils

Virtual environment
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

Verify 
python3 -c "import aiohttp; print('OK')"

python3 main.py

-------------------------------
On another computer/pi

Create the storage directory:
sudo mkdir -p /srv/video-storage
sudo chmod 777 /srv/video-storage

Start the server

From video-server/:
docker compose up -d --build

Check:
curl http://localhost/api/health

# Test cleanup
curl -X POST \
  -H "Authorization: Bearer archery-video-2026" \
  http://localhost/api/cleanup

Autostart node:
Now make the node auto-start on boot using systemd.

sudo nano /etc/systemd/system/video-node.service

sudo systemctl daemon-reload
sudo systemctl enable video-node
sudo systemctl start video-node


Check status
systemctl status video-node
Watch logs
journalctl -u video-node -f
Test after reboot
sudo reboot

Then check:

curl http://localhost:8080/health

and from the server:

curl http://SERVER_PI_IP/api/nodes
---------------------

1. Docker Compose already auto-starts containers

Because we used:

restart: unless-stopped

After reboot, Docker should restart:

PostgreSQL
FastAPI app

Verify:

docker ps
2. Enable Docker at boot
sudo systemctl enable docker
sudo systemctl start docker
3. Reboot test
sudo reboot

Then check:

curl http://localhost/api/health

and from another computer:

http://SERVER_PI_IP/
4. Useful server commands

From the video-server directory:

docker compose ps
docker compose logs -f app
docker compose logs -f postgres

Restart server app:

docker compose restart app

Stop everything:

docker compose down

Start again:

docker compose up -d
5. Check video storage
du -sh /srv/video-storage

List stored videos:

find /srv/video-storage -name "*.mp4" | head
