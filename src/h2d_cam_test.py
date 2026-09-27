import os
import cv2

# Force ffmpeg backend to ignore SSL certificate validation
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|tls_verify;0"

# Replace with 'bblp' or 'bbla'
USER = "bblp"  
PASS = "6ef79e8d"
IP = "192.168.0.100"

rtsp_url = f"rtsps://{USER}:{PASS}@{IP}:322/live/0"

cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)

if cap.isOpened():
    ret, frame = cap.read()
    if ret:
        cv2.imwrite("snapshot.jpg", frame)
        print("Successfully captured frame!")
    else:
        print("Failed to grab frame.")
    cap.release()
else:
    print("Failed to open video stream.")