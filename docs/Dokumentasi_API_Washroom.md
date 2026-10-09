---
title: "Dokumentasi API Washroom"
subtitle: "Panduan Integrasi dan Pengujian Postman"
author: "People Counting Middleware"
---

Dokumen ini menjelaskan endpoint API yang dipakai untuk mengirim data sensor toilet dan rating pelanggan dari sistem eksternal ke server middleware.

Base URL server saat ini:

- http://202.157.177.157:8080

Semua request ke endpoint API memerlukan header berikut:

- X-API-Key: `<API key>`

> API key diberikan terpisah oleh tim kami melalui jalur aman. Jangan membagikan key ini; hubungi kami bila key bocor agar diganti.

Catatan penting:

- Data sensor (`/api/v1/readings/`) memakai format **Washroom Dashboard Raw Data Documentation v1.0** dari tim sensor: `id`, `inputDate`, `deviceId`, `value`, `battery`, `lastOnline`, `status`. Data dikirim setiap 30 menit.
- Jenis sensor tidak dikirim di payload: jenisnya diambil dari tipe device (`deviceId`) yang didaftarkan di Admin `DeviceList`.
- Lokasi device di dashboard mengikuti hierarki **Client → Region → Site → Area → Scope** (satu Scope = satu toilet) yang diatur di Admin. Response API menyertakan lokasi ini di field `location` (`null` jika device belum dipasangkan ke Scope). Field `building` / `floor` / `gender` masih dikirim untuk kompatibilitas.
- `deviceId` / `device_id` WAJIB sudah terdaftar di Admin `DeviceList`. API TIDAK membuat device baru otomatis; device yang tidak terdaftar atau tipenya tidak cocok ditolak dengan `400`.
- URL boleh dengan atau tanpa garis miring di akhir (`/api/v1/readings/` atau `/api/v1/readings`).

Server hanya menyimpan hash dari API key, sehingga key tidak dapat ditampilkan ulang. Jika key hilang, minta key baru ke tim kami (key lama otomatis tidak berlaku).

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

Digunakan untuk mengambil history data sensor. Field response memakai nama yang sama dengan payload, ditambah `reading_id` (ID di server), `type` dan lokasi device dari Admin.

### Query params

- type: soap | toilet-paper | tissue | trash | ammonia
- deviceId (atau device_id): ID perangkat
- building: contoh `GRAHA ISS BINTARO`
- floor: contoh `2`
- gender: male | female
- scope: ID Scope (toilet) di hierarki lokasi
- time_from: ISO 8601, inclusive (berdasarkan `inputDate`)
- time_to: ISO 8601, inclusive

### Contoh request

```http
GET /api/v1/readings/?type=soap&deviceId=POSTMAN-SOAP-01 HTTP/1.1
Host: 202.157.177.157:8080
X-API-Key: <API key>
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
      "id": "1",
      "deviceId": "POSTMAN-SOAP-01",
      "type": "soap",
      "building": "GRAHA ISS BINTARO",
      "floor": "2",
      "gender": "male",
      "location": {
        "client": "ISS", "region": "Banten", "site": "Bintaro", "area": "Graha ISS",
        "scope": "Floor 2 - Toilet Pria", "scope_id": 1
      },
      "inputDate": "2026-10-06T10:15:30+07:00",
      "value": 60.0,
      "battery": 81,
      "lastOnline": "2026-10-06T10:15:30+07:00",
      "status": "Terisi",
      "severity": "normal"
    }
  ]
}
```

---

## 3. Endpoint: POST /api/v1/readings/

Digunakan tim sensor untuk mengirim data sensor (Amonia, Liquid Soap, Tissue Paper, Toilet Paper, Trash Level), satu object atau list maksimal 500 item, setiap 30 menit.

### Field

| Field | Tipe | Wajib | Keterangan |
| --- | --- | --- | --- |
| `id` | String | Ya | ID unik data, **unik per device**. Disimpan di server. |
| `inputDate` | DateTime (ISO 8601) | Ya | Waktu data dicatat. Tanpa offset dibaca sebagai WIB; `Z` = UTC. Maksimal 5 menit di masa depan. |
| `deviceId` | String | Ya | ID device, harus terdaftar di Admin `DeviceList`. Tipe device di Admin menentukan jenis sensor. |
| `value` | Numeric | Tidak | Nilai bacaan sensor, disimpan apa adanya (% untuk soap/tissue/toilet paper/trash, ppm untuk amonia). |
| `battery` | Numeric | Tidak | Sisa baterai 0-100 (%). |
| `lastOnline` | DateTime (ISO 8601) | Tidak | Terakhir kali device online. Maksimal 5 menit di masa depan. |
| `status` | String | Tidak | Kondisi menurut tim sensor (mis. `Terisi`), disimpan dan ditampilkan apa adanya di dashboard. Jika kosong, server menentukan kondisi dari `value` (lihat bagian Status dan severity). |

### Payload satu object

```json
{
  "id": "1",
  "inputDate": "2026-08-19T10:15:30Z",
  "deviceId": "POSTMAN-SOAP-01",
  "value": 60,
  "battery": 81,
  "lastOnline": "2026-08-19T10:15:30Z",
  "status": "Terisi"
}
```

### Payload list (batch)

```json
[
  {"id": "101", "inputDate": "2026-10-06T10:00:00+07:00", "deviceId": "POSTMAN-SOAP-01",
   "value": 45, "battery": 90, "lastOnline": "2026-10-06T10:00:00+07:00", "status": "Terisi"},
  {"id": "102", "inputDate": "2026-10-06T10:00:00+07:00", "deviceId": "POSTMAN-TOILET-PAPER-01",
   "value": 36, "battery": 88, "lastOnline": "2026-10-06T10:00:00+07:00", "status": "Terisi"},
  {"id": "103", "inputDate": "2026-10-06T10:00:00+07:00", "deviceId": "POSTMAN-TISSUE-01",
   "value": 28, "battery": 80, "lastOnline": "2026-10-06T10:00:00+07:00", "status": "Hampir Habis"},
  {"id": "104", "inputDate": "2026-10-06T10:00:00+07:00", "deviceId": "POSTMAN-TRASH-01",
   "value": 72, "battery": 85, "lastOnline": "2026-10-06T10:00:00+07:00", "status": "Hampir Penuh"},
  {"id": "105", "inputDate": "2026-10-06T10:00:00+07:00", "deviceId": "POSTMAN-AMMONIA-01",
   "value": 12.5, "battery": 91, "lastOnline": "2026-10-06T10:00:00+07:00", "status": "Bau"}
]
```

### Validasi

- `id`, `inputDate`, `deviceId` wajib
- `deviceId` harus sudah ada di Admin `DeviceList` dengan tipe `soap`, `toilet-paper`, `tissue`, `trash` atau `ammonia`; device lain (people counter, satisfaction) ditolak
- `battery` 0-100 (opsional); `value` dan `status` tidak dibatasi
- `inputDate` dan `lastOnline` tidak boleh lebih dari 5 menit di masa depan (toleransi selisih jam device)
- **Data ganda:** jika `id` yang sama untuk `deviceId` yang sama sudah tersimpan, data tersebut dilewati (tidak disimpan ulang dan tidak error), jadi aman untuk mengirim ulang
- Batch: semua item harus valid, jika satu item gagal maka seluruh batch ditolak

### Contoh response sukses

HTTP `201` jika ada minimal satu data baru, `200` jika semua data sudah pernah tersimpan. `created` = jumlah data baru, `duplicates` = jumlah data yang dilewati. Untuk batch, `data` berupa list sesuai urutan payload.

```json
{
  "status": "success",
  "created": 1,
  "duplicates": 0,
  "data": {
    "reading_id": 11,
    "id": "1",
    "deviceId": "POSTMAN-SOAP-01",
    "type": "soap",
    "building": "GRAHA ISS BINTARO",
    "floor": "2",
    "gender": "male",
    "location": {
      "client": "ISS", "region": "Banten", "site": "Bintaro", "area": "Graha ISS",
      "scope": "Floor 2 - Toilet Pria", "scope_id": 1
    },
    "inputDate": "2026-08-19T17:15:30+07:00",
    "value": 60.0,
    "battery": 81,
    "lastOnline": "2026-08-19T17:15:30+07:00",
    "status": "Terisi",
    "severity": "normal"
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
        "deviceId": ["Device ID tidak terdaftar di admin. Harap daftarkan device terlebih dahulu."]
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
- scope: ID Scope (toilet)
- time_from
- time_to

### Contoh request

```http
GET /api/v1/customer-responses/?device_id=POSTMAN-FEEDBACK-01 HTTP/1.1
Host: 202.157.177.157:8080
X-API-Key: <API key>
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
  "id": "press-0001",
  "device_id": "POSTMAN-FEEDBACK-01",
  "rating": 5,
  "comment": "Toilet bersih"
}
```

> `device_id` untuk feedback juga harus sudah ada di Admin dan tipe `satisfaction`.
> `id` opsional, tetapi disarankan: rating dengan `id` yang sama untuk `device_id` yang sama hanya disimpan sekali, jadi aman dikirim ulang.

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
- `id` opsional, unik per `device_id`; rating yang `id`-nya sudah tersimpan dilewati (dihitung di `duplicates`)
- `time` opsional (default waktu server menerima), tidak boleh lebih dari 5 menit di masa depan
- Batch: seluruh item harus valid, bila salah satu gagal semua batch ditolak

### Contoh response sukses

```json
{
  "status": "success",
  "created": 1,
  "duplicates": 0,
  "data": {
    "response_id": 4,
    "id": "press-0001",
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

## 6. Status dan severity

`status` yang ditampilkan di dashboard adalah `status` yang dikirim tim sensor, apa adanya.

`severity` (`normal` / `warning` / `critical`) diisi server bila `status` sama dengan nama kondisi di Admin **Status rules** untuk jenis sensor tersebut (tidak membedakan huruf besar/kecil), misalnya `Terisi` → `normal`, `Habis` → `critical`. Jika tidak ada yang cocok, `severity` ditentukan dari `value` memakai rentang di Status rules.

Jika `status` tidak dikirim (atau kosong), server menentukan `status` dan `severity` dari `value` memakai Status rules, misalnya sabun `value: 0` → `Habis` (`critical`). Jika `status` dan `value` sama-sama kosong, keduanya tetap kosong.

---

## 7. Dokumentasi Swagger dan schema

Server menyediakan dokumentasi OpenAPI secara public tanpa login.

- Swagger UI: http://202.157.177.157:8080/api/docs/
- Schema YAML: http://202.157.177.157:8080/api/schema/

---

## 8. Contoh request cURL

### POST reading

```bash
curl -X POST http://202.157.177.157:8080/api/v1/readings/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <API key>" \
  -d '{
    "id": "1",
    "inputDate": "2026-10-06T10:15:30+07:00",
    "deviceId": "POSTMAN-SOAP-01",
    "value": 60,
    "battery": 81,
    "lastOnline": "2026-10-06T10:15:30+07:00",
    "status": "Terisi"
  }'
```

### POST rating

```bash
curl -X POST http://202.157.177.157:8080/api/v1/customer-responses/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <API key>" \
  -d '{
    "device_id": "POSTMAN-FEEDBACK-01",
    "rating": 5,
    "comment": "Toilet bersih"
  }'
```

---

## 9. Catatan penting untuk testing Postman

- Gunakan header `X-API-Key` dan bukan `Authorization`
- `deviceId` / `device_id` harus sudah terdaftar di Admin `DeviceList` (dengan tipe sensor yang benar) sebelum mengirim data
- Jika device tidak terdaftar atau tipenya tidak cocok, server menolak request dengan HTTP 400
- Untuk mengulang test data sensor, ganti `id`; `id` yang sama untuk device yang sama dilewati sebagai duplikat

---

## 10. Contoh payload valid untuk monitor toilet

```json
{
  "id": "2001",
  "inputDate": "2026-10-06T10:30:00+07:00",
  "deviceId": "POSTMAN-SOAP-01",
  "value": 33,
  "battery": 89,
  "lastOnline": "2026-10-06T10:30:00+07:00",
  "status": "Hampir Habis"
}
```

```json
{
  "device_id": "POSTMAN-FEEDBACK-01",
  "rating": 4,
  "comment": "Cukup bersih"
}
```
