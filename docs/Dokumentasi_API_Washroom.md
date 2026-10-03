---
title: "Dokumentasi API Washroom"
subtitle: "Panduan Integrasi dan Pengujian Postman"
author: "People Counting Middleware"
---

Dokumen ini menjelaskan endpoint API yang dipakai untuk mengirim data sensor toilet dan rating pelanggan dari sistem eksternal ke server middleware.

Base URL server saat ini:

- https://unpopular-unwieldy-tables.ngrok-free.dev

Semua request ke endpoint API memerlukan header berikut:

- X-API-Key: lt4lP2mgc65ktGoNCDtfQ65BkAp0V-HIedfU6RqAm8M

> Demo key aktif untuk collection Postman: `lt4lP2mgc65ktGoNCDtfQ65BkAp0V-HIedfU6RqAm8M`

Catatan penting:

- Endpoint POST harus memakai trailing slash, misalnya `/api/v1/readings/` bukan `/api/v1/readings`
- `device_id` WAJIB sudah terdaftar di Admin `DeviceList`.
- API TIDAK BOLEH membuat device baru otomatis.
- Jika `device_id` tidak ditemukan atau tipe device tidak cocok, server akan menolak dengan `400` dan pesan error yang jelas.

Nilai X-API-Key didapat dari admin Django, pada menu Washroom API > API client. Kunci yang dibuat akan ditampilkan sekali dan hanya hash-nya yang disimpan.

Device uji untuk Postman sudah terdaftar di Admin `DeviceList` dengan lokasi `GRAHA ISS BINTARO`, lantai `2`, gender `male`:

| Tipe | `device_id` |
| --- | --- |
| Customer Satisfaction | `POSTMAN-FEEDBACK-01` |
| Soap | `POSTMAN-SOAP-01` |
| Toilet Paper | `POSTMAN-TOILET-PAPER-01` |
| Tissue | `POSTMAN-TISSUE-01` |
| Trash | `POSTMAN-TRASH-01` |
| Amonia | `POSTMAN-AMMONIA-01` |

Import collection dari `Dokumentasi_API_Washroom.postman_collection.json`. Collection berisi request satuan dan batch untuk kelima sensor, serta request rating Customer Satisfaction.

---

## 1. Autentikasi

Semua endpoint di bawah /api/v1/ memerlukan autentikasi.

Jika key tidak valid atau tidak aktif, server akan mengembalikan:

```json
{
  "status": "error",
  "message": "Authentication credentials were not provided."
}
```

Response status:

- 401 Unauthorized

---

## 2. Endpoint: GET /api/v1/readings/

Digunakan untuk mengambil history data sensor.

### Query params

- type: soap | toilet-paper | tissue | trash | ammonia
- device_id: ID perangkat
- building: contoh `GRAHA ISS BINTARO`
- floor: contoh `2`
- gender: male | female
- time_from: ISO 8601, inclusive
- time_to: ISO 8601, inclusive

### Contoh request

```http
GET /api/v1/readings/?type=soap&device_id=POSTMAN-SOAP-01 HTTP/1.1
Host: unpopular-unwieldy-tables.ngrok-free.dev
X-API-Key: lt4lP2mgc65ktGoNCDtfQ65BkAp0V-HIedfU6RqAm8M
```

### Contoh response

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "reading_id": 11,
      "device_id": "POSTMAN-SOAP-01",
      "type": "soap",
      "building": "GRAHA ISS BINTARO",
      "floor": "2",
      "gender": "male",
      "time": "2026-10-03T08:14:00+07:00",
      "battery": 90,
      "level": 45.0,
      "condition": "Hampir Habis",
      "severity": "warning"
    }
  ]
}
```

---

## 3. Endpoint: POST /api/v1/readings/

Digunakan untuk mengirim satu data sensor atau batch maksimal 500 item.

### Payload satu object

```json
{
  "device_id": "POSTMAN-SOAP-01",
  "type": "soap",
  "battery": 90,
  "level": 45
}
```

> `device_id` harus sudah ada di Admin `DeviceList` dan tipe harus sesuai. Jika device belum terdaftar, request akan ditolak.

### Payload list (batch)

```json
[
  {
    "device_id": "POSTMAN-SOAP-01",
    "type": "soap",
    "level": 45,
    "battery": 90
  },
  {
    "device_id": "POSTMAN-TOILET-PAPER-01",
    "type": "toilet-paper",
    "level": 36,
    "battery": 88
  },
  {
    "device_id": "POSTMAN-TISSUE-01",
    "type": "tissue",
    "level": 28,
    "battery": 80
  },
  {
    "device_id": "POSTMAN-TRASH-01",
    "type": "trash",
    "level": 72,
    "battery": 85
  },
  {
    "device_id": "POSTMAN-AMMONIA-01",
    "type": "ammonia",
    "level": 12.5,
    "battery": 91
  }
]
```

### Validasi

- `device_id` wajib string
- `device_id` harus sudah ada di Admin `DeviceList`; API TIDAK BOLEH auto-create device baru
- `type` harus salah satu: `soap`, `toilet-paper`, `tissue`, `trash`, `ammonia`
- `battery` 0-100 (opsional)
- `level` 0-100 untuk type non-ammonia; ammonia dapat lebih dari 100 karena satuannya ppm
- `time` bersifat opsional; jika tidak dikirim, server memakai waktu server saat request masuk
- `time` tanpa offset akan dibaca sebagai WIB
- Jika `device_id` ada tetapi tipe device-nya berbeda, request akan ditolak
- Batch: semua item harus valid, jika satu item gagal maka seluruh batch ditolak

### Contoh response sukses

```json
{
  "status": "success",
  "data": {
    "reading_id": 11,
    "device_id": "POSTMAN-SOAP-01",
    "type": "soap",
    "building": "GRAHA ISS BINTARO",
    "floor": "2",
    "gender": "male",
    "time": "2026-10-03T08:14:00+07:00",
    "battery": 90,
    "level": 45,
    "condition": "Hampir Habis",
    "severity": "warning"
  }
}
```

### Contoh response error

```json
{
  "status": "error",
  "message": "Data tidak valid",
  "errors": [
    {
      "index": 1,
      "errors": {
        "device_id": ["This field is required."]
      }
    }
  ]
}
```

---

## 4. Endpoint: GET /api/v1/customer-responses/

Digunakan untuk mengambil history rating pelanggan.

### Query params

- device_id: ID perangkat
- building
- floor
- gender
- time_from
- time_to

### Contoh request

```http
GET /api/v1/customer-responses/?device_id=POSTMAN-FEEDBACK-01 HTTP/1.1
Host: unpopular-unwieldy-tables.ngrok-free.dev
X-API-Key: lt4lP2mgc65ktGoNCDtfQ65BkAp0V-HIedfU6RqAm8M
```

### Contoh response

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "response_id": 4,
      "device_id": "POSTMAN-FEEDBACK-01",
      "building": "GRAHA ISS BINTARO",
      "floor": "2",
      "gender": "male",
      "time": "2026-10-03T08:20:00+07:00",
      "rating": 5,
      "comment": "bersih"
    }
  ]
}
```

---

## 5. Endpoint: POST /api/v1/customer-responses/

Digunakan untuk mengirim rating pelanggan (skala 1-5) dari tombol feedback.

### Payload satu object

```json
{
  "device_id": "POSTMAN-FEEDBACK-01",
  "rating": 5,
  "comment": "Toilet bersih"
}
```

> `device_id` untuk feedback juga harus sudah ada di Admin dan tipe `satisfaction`.

### Payload list

```json
[
  {
    "device_id": "POSTMAN-FEEDBACK-01",
    "rating": 5,
    "comment": "Toilet bersih"
  },
  {
    "device_id": "POSTMAN-FEEDBACK-01",
    "rating": 3,
    "comment": "Cukup bersih"
  }
]
```

### Validasi

- `device_id` wajib string
- `device_id` harus sudah ada di Admin `DeviceList` dan tipe `satisfaction`
- `rating` wajib integer 1 sampai 5
- `comment` opsional string kosong
- Batch: seluruh item harus valid, bila salah satu gagal semua batch ditolak

### Contoh response sukses

```json
{
  "status": "success",
  "data": {
    "response_id": 4,
    "device_id": "POSTMAN-FEEDBACK-01",
    "building": "GRAHA ISS BINTARO",
    "floor": "2",
    "gender": "male",
    "time": "2026-10-03T08:20:00+07:00",
    "rating": 5,
    "comment": "Toilet bersih"
  }
}
```

---

## 6. Status rule / kondisi perangkat

Server menghitung condition berdasarkan status rule yang terdapat di admin.

Contoh kondisi:

- `soap`: `Terisi`, `Hampir Habis`, `Habis`
- `trash`: `Normal`, `Penuh`
- `ammonia`: `Normal`, `Bahaya`

Setiap rule mempunyai:

- `min_level` inklusif
- `max_level` eksklusif

Artinya kondisi terpilih adalah rule dengan range yang memenuhi:

```text
min_level <= level < max_level
```

Severity:

- normal
- warning
- critical

---

## 7. Dokumentasi Swagger dan schema

Server menyediakan dokumentasi OpenAPI secara public tanpa login.

- Swagger UI: https://unpopular-unwieldy-tables.ngrok-free.dev/api/docs/
- Schema YAML: https://unpopular-unwieldy-tables.ngrok-free.dev/api/schema/

---

## 8. Contoh request cURL

### POST reading

```bash
curl -X POST https://unpopular-unwieldy-tables.ngrok-free.dev/api/v1/readings/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: lt4lP2mgc65ktGoNCDtfQ65BkAp0V-HIedfU6RqAm8M" \
  -d '{
    "device_id": "POSTMAN-SOAP-01",
    "type": "soap",
    "battery": 92,
    "level": 44
  }'
```

### POST rating

```bash
curl -X POST http://192.168.10.120:8080/api/v1/customer-responses/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: lt4lP2mgc65ktGoNCDtfQ65BkAp0V-HIedfU6RqAm8M" \
  -d '{
    "device_id": "POSTMAN-FEEDBACK-01",
    "rating": 5,
    "comment": "Toilet bersih"
  }'
```

---

## 9. Catatan penting untuk testing Postman

- Gunakan header `X-API-Key` dan bukan `Authorization`
- `device_id` harus sudah terdaftar di Admin `DeviceList` sebelum mengirim data
- Jika `device_id` tidak terdaftar atau tipenya tidak cocok dengan payload, server menolak request dengan HTTP 400
- Untuk testing lokal, gunakan IP server berikut:
  - http://192.168.10.120:8080
- Jika Anda ingin test dummy work order bukan washroom API, gunakan:
  - http://192.168.10.120:8080/dummy/api_iot.php

---

## 10. Contoh payload valid untuk monitor toilet

```json
{
  "device_id": "POSTMAN-SOAP-01",
  "type": "soap",
  "battery": 89,
  "level": 33
}
```

```json
{
  "device_id": "POSTMAN-FEEDBACK-01",
  "rating": 4,
  "comment": "Cukup bersih"
}
```
