# Python'ning barqaror va yengil versiyasini tanlaymiz
FROM python:3.10-slim

# Konteyner ichidagi ishchi papkani belgilaymiz
WORKDIR /app

# Muhit o'zgaruvchilari (Python kesh yozmasligi va srazu konsolga chiqarishi uchun)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Rasmlar bilan ishlash (Pillow) uchun kerakli tizim paketlarini o'rnatamiz
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Birinchi talab qilinadigan kutubxonalar ro'yxatini ko'chiramiz (Keshni tezlashtirish uchun)
COPY requirements.txt .

# Kutubxonalarni o'rnatamiz
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Qolgan barcha kodlarni konteynerga nusxalaymiz
COPY . .

# Botni ishga tushirish komandasi
CMD ["python", "main.py"]