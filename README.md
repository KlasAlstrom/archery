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

