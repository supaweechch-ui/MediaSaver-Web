# MediaSaver Web

เว็บโหลดไฟล์ **mp3 / m4a / flac / wav / opus** และ **mp4 / webm / mkv** จากลิงก์ YouTube, TikTok และเว็บอื่นที่ yt-dlp รองรับ
วางลิงก์ → เลือกฟอร์แมต → กดบันทึกไฟล์ ใช้ได้จากมือถือ ไม่ต้องรัน Python เอง

- แบ็กเอนด์: Flask + yt-dlp + ffmpeg (ไฟล์เดียว `app.py`)
- หน้าเว็บ: `static/index.html` (ไฟล์เดียว ไม่ต้อง build)
- ฟีเจอร์: เลือกความละเอียด/บิตเรต, ตัดช่วงเวลา, playlist (รวมเป็น .zip), ฝังปก+metadata, แถบความคืบหน้า, รหัสผ่านเข้าใช้งาน

## Deploy บน Railway (แนะนำ)

1. อัปโหลดโฟลเดอร์นี้ขึ้น GitHub (repo ใหม่)
2. Railway → **New Project** → **Deploy from GitHub repo** เลือก repo นี้ (Railway จะเจอ `Dockerfile` เอง)
3. แท็บ **Variables** เพิ่ม `ACCESS_PASSWORD` = รหัสที่คุณตั้ง (**ควรตั้ง** ไม่งั้นใครมีลิงก์ก็ใช้เซิร์ฟเวอร์คุณได้)
4. แท็บ **Settings → Networking → Generate Domain** แล้วเปิดลิงก์ที่ได้ในมือถือ
5. เพิ่มลงหน้าจอหลัก: เมนูเบราว์เซอร์ → Add to Home screen

> ใช้ Vercel ไม่ได้ เพราะ yt-dlp/ffmpeg โหลดนานเกินเวลาจำกัดของ function

## รันเองด้วย Docker (เครื่องที่บ้าน/VPS)

```
docker compose up --build
```
เปิด http://localhost:8080 (แก้รหัสผ่านใน `docker-compose.yml` ก่อน)

## ถ้า YouTube ขึ้น "บล็อกเซิร์ฟเวอร์" (ต้องใช้ cookies)

IP ของ cloud มักโดน YouTube ถามว่าเป็นบอทหรือเปล่า แก้ด้วย cookies จากบัญชีของคุณ:

1. ใน Chrome ติดตั้งส่วนขยายส่งออก cookies แบบ Netscape (เช่น "Get cookies.txt LOCALLY")
2. เปิด youtube.com ขณะล็อกอิน (แนะนำใช้บัญชีสำรอง) แล้ว export เป็น `cookies.txt`
3. แปลงเป็น base64 แล้วใส่ตัวแปร `COOKIES_B64`
   - Linux/Mac: `base64 -w0 cookies.txt`
   - Termux: `base64 -w0 cookies.txt`
4. Redeploy

cookies = สิทธิ์เข้าบัญชีของคุณ เก็บเป็นความลับ อย่าใส่ใน GitHub

## ตัวแปรสภาพแวดล้อม

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `ACCESS_PASSWORD` | (ว่าง = ไม่ล็อก) | รหัสผ่านเข้าใช้งาน |
| `MAX_CONCURRENT` | 2 | งานที่โหลดพร้อมกัน |
| `FILE_TTL_MIN` | 30 | ลบไฟล์หลังเสร็จกี่นาที |
| `MAX_PLAYLIST` | 50 | คลิปสูงสุดต่อ playlist |
| `MAX_DURATION_MIN` | 180 | ความยาวคลิปสูงสุด (0 = ไม่จำกัด) |
| `MAX_FILESIZE_MB` | 1024 | ขนาดไฟล์สูงสุด (0 = ไม่จำกัด) |
| `COOKIES_B64` | – | cookies.txt แบบ base64 |

## อัปเดต yt-dlp

เว็บต้นทางเปลี่ยนบ่อย ถ้าโหลดไม่ได้กะทันหัน ให้ **Redeploy** (Railway: Deployments → Redeploy แบบล้างแคช) เพื่อดึง yt-dlp ล่าสุด
เวอร์ชันที่ใช้อยู่แสดงที่ท้ายหน้าเว็บ

## หมายเหตุ

- งานและไฟล์เก็บในหน่วยความจำ/ดิสก์ชั่วคราว รีสตาร์ตเซิร์ฟเวอร์แล้วงานที่ค้างจะหาย
- ใช้โหลดเนื้อหาที่คุณมีสิทธิ์เท่านั้น และอย่าเปิดสาธารณะโดยไม่ตั้งรหัสผ่าน
