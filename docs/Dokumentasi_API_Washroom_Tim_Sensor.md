---
title: "Dokumentasi API Washroom"
subtitle: "Panduan Pengiriman Data Sensor"
author: "People Counting Middleware"
---

Dokumen ini menjelaskan cara mengirim data sensor toilet (Amonia, Liquid Soap, Tissue Paper, Toilet Paper dan Trash Level) ke server kami. Format data mengikuti **Washroom Dashboard Raw Data Documentation v1.0**.

---

## 1. Informasi Umum

| | |
| --- | --- |
| Base URL | `http://202.157.177.157:8080` |
| Endpoint | `POST /api/v1/readings/` |
| Content-Type | `application/json` |
| Autentikasi | Header `X-API-Key: <API key>` |
| Frekuensi kirim | Setiap 30 menit |

- **API key** diberikan terpisah oleh tim kami melalui jalur aman. Jangan membagikan key ini; hubungi kami bila key hilang atau bocor agar diganti (key lama otomatis tidak berlaku).
- Kirim key di header `X-API-Key`, **bukan** di header `Authorization`.
- **ID device harus didaftarkan terlebih dahulu oleh tim kami.** Kirimkan daftar `deviceId` beserta jenis sensornya (amonia, sabun, tisu, tisu toilet, tempat sampah) sebelum mulai mengirim data. Data dari device yang belum terdaftar akan ditolak.
- Jenis sensor **tidak perlu** dikirim di payload; server mengenalinya dari `deviceId` yang sudah terdaftar.
- URL boleh dengan atau tanpa garis miring di akhir (`/api/v1/readings/` atau `/api/v1/readings`).

---

## 2. Field Data

| Field | Tipe | Wajib | Keterangan |
| --- | --- | --- | --- |
| `id` | String | Ya | ID unik data, **unik per device**. |
| `inputDate` | DateTime (ISO 8601) | Ya | Waktu data dicatat. Tanpa zona waktu dibaca sebagai WIB; akhiran `Z` = UTC. Maksimal 5 menit di masa depan. |
| `deviceId` | String | Ya | ID device yang sudah didaftarkan oleh tim kami. |
| `value` | Numeric | Tidak | Nilai bacaan sensor (% untuk sabun, tisu, tisu toilet, tempat sampah; ppm untuk amonia). |
| `battery` | Numeric | Tidak | Sisa baterai device, 0 sampai 100 (%). |
| `lastOnline` | DateTime (ISO 8601) | Tidak | Terakhir kali device online. Maksimal 5 menit di masa depan. |
| `status` | String | Tidak | Kondisi sensor hasil pembacaan (mis. `Terisi`, `Hampir Habis`), ditampilkan apa adanya di dashboard. Jika kosong, kondisi ditentukan server dari `value`. |

---

## 3. Contoh Request

Contoh `deviceId` di bawah (`DEVICE-SOAP-01`, dan seterusnya) hanya ilustrasi; gunakan ID device Anda yang sudah terdaftar.

### 3.1 Satu data

```json
{
  "id": "1",
  "inputDate": "2026-10-06T10:15:30+07:00",
  "deviceId": "DEVICE-SOAP-01",
  "value": 60,
  "battery": 81,
  "lastOnline": "2026-10-06T10:15:30+07:00",
  "status": "Terisi"
}
```

### 3.2 Banyak data sekaligus (batch)

Kirim sebagai JSON list, maksimal **500 data** per request.

```json
[
  {"id": "101", "inputDate": "2026-10-06T10:00:00+07:00", "deviceId": "DEVICE-SOAP-01",
   "value": 45, "battery": 90, "lastOnline": "2026-10-06T10:00:00+07:00", "status": "Terisi"},
  {"id": "102", "inputDate": "2026-10-06T10:00:00+07:00", "deviceId": "DEVICE-TOILET-PAPER-01",
   "value": 36, "battery": 88, "lastOnline": "2026-10-06T10:00:00+07:00", "status": "Terisi"},
  {"id": "103", "inputDate": "2026-10-06T10:00:00+07:00", "deviceId": "DEVICE-TISSUE-01",
   "value": 28, "battery": 80, "lastOnline": "2026-10-06T10:00:00+07:00", "status": "Hampir Habis"},
  {"id": "104", "inputDate": "2026-10-06T10:00:00+07:00", "deviceId": "DEVICE-TRASH-01",
   "value": 72, "battery": 85, "lastOnline": "2026-10-06T10:00:00+07:00", "status": "Hampir Penuh"},
  {"id": "105", "inputDate": "2026-10-06T10:00:00+07:00", "deviceId": "DEVICE-AMMONIA-01",
   "value": 12.5, "battery": 91, "lastOnline": "2026-10-06T10:00:00+07:00", "status": "Bau"}
]
```

### 3.3 Contoh cURL

```bash
curl -X POST http://202.157.177.157:8080/api/v1/readings/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <API key>" \
  -d '{
    "id": "1",
    "inputDate": "2026-10-06T10:15:30+07:00",
    "deviceId": "DEVICE-SOAP-01",
    "value": 60,
    "battery": 81,
    "lastOnline": "2026-10-06T10:15:30+07:00",
    "status": "Terisi"
  }'
```

---

## 4. Aturan Validasi

- `id`, `inputDate` dan `deviceId` wajib diisi.
- `deviceId` harus sudah terdaftar sebagai device sensor (amonia, sabun, tisu, tisu toilet atau tempat sampah).
- `battery` harus angka 0 sampai 100; `value` harus angka.
- `inputDate` dan `lastOnline` tidak boleh lebih dari 5 menit di masa depan (toleransi selisih jam device). Pastikan jam device sudah sinkron.
- **Data ganda:** data dengan `id` yang sama untuk `deviceId` yang sama tidak disimpan ulang dan tidak dianggap error. Aman untuk mengirim ulang data yang sama, misalnya setelah koneksi terputus.
- **Batch:** semua data harus valid. Jika satu data tidak valid, seluruh batch ditolak dan tidak ada yang disimpan; perbaiki data tersebut lalu kirim ulang batch-nya.

---

## 5. Response

### 5.1 Berhasil

| HTTP | Arti |
| --- | --- |
| `201 Created` | Minimal satu data baru tersimpan. |
| `200 OK` | Semua data sudah pernah diterima sebelumnya (duplikat), tidak ada yang disimpan ulang. |

`created` = jumlah data baru yang tersimpan, `duplicates` = jumlah data duplikat yang dilewati. Field `data` berisi data yang diterima server (untuk batch berupa list sesuai urutan kiriman); cukup periksa `status`, `created` dan `duplicates`.

```json
{
  "status": "success",
  "created": 5,
  "duplicates": 0,
  "data": [ ... ]
}
```

### 5.2 Gagal

Semua error memakai format yang sama: `status`, `message`, dan (untuk data tidak valid) `errors` yang menunjukkan urutan data (`index`, mulai dari 0) beserta field yang bermasalah.

| HTTP | Penyebab | Contoh `message` |
| --- | --- | --- |
| `400 Bad Request` | Data tidak valid, device belum terdaftar, body bukan JSON, atau lebih dari 500 data | `Data tidak valid` |
| `401 Unauthorized` | Header `X-API-Key` tidak dikirim | `API key wajib dikirim di header X-API-Key` |
| `401 Unauthorized` | API key salah atau sudah tidak aktif | `API key tidak valid` |

Contoh data ke-2 dalam batch (`index` 1) memakai device yang belum terdaftar:

```json
{
  "status": "error",
  "message": "Data tidak valid",
  "errors": [
    {
      "index": 1,
      "errors": {
        "deviceId": ["Device ID tidak terdaftar di admin. Harap daftarkan device terlebih dahulu."]
      }
    }
  ]
}
```

Contoh field wajib tidak dikirim:

```json
{
  "status": "error",
  "message": "Data tidak valid",
  "errors": [
    {
      "index": 0,
      "errors": {
        "id": ["This field is required."],
        "inputDate": ["This field is required."]
      }
    }
  ]
}
```

---

## 6. Rekomendasi

- Jika request gagal karena koneksi atau server tidak merespons, kirim ulang data yang sama dengan `id` yang sama; data tidak akan tercatat dua kali.
- Jika menerima `400`, periksa `errors` untuk mengetahui data dan field yang perlu diperbaiki. Jika menerima `401`, periksa API key.
- Hubungi tim kami untuk pendaftaran device baru, penggantian API key, atau jika menemui kendala.
