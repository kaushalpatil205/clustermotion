import http from "k6/http";
import { check, sleep } from "k6";
import { uuidv4 } from "https://jslib.k6.io/k6-utils/1.4.0/index.js";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------
const BASE = __ENV.BASE_URL || "http://localhost:8080";
const SKUS = ["sku-100", "SKU-002", "SKU-003", "SKU-004", "SKU-005"];

export const options = {
  scenarios: {
    shop: {
      executor: "constant-arrival-rate",
      rate: 20,
      timeUnit: "1s",
      duration: __ENV.DURATION || "30m",
      preAllocatedVUs: 10,
      maxVUs: 50,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<500"],
  },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const headers = { "User-Agent": "shop-loadgen/1.0" };

function catalogRead() {
  const r = Math.random();
  if (r < 0.3) {
    const sku = SKUS[Math.floor(Math.random() * SKUS.length)];
    return http.get(`${BASE}/api/catalog/products/${sku}`, { headers });
  }
  return http.get(`${BASE}/api/catalog/products`, { headers });
}

function orderCreate() {
  const key = uuidv4();
  const sku = SKUS[Math.floor(Math.random() * SKUS.length)];
  const body = JSON.stringify({ sku, qty: 1 });
  const opts = {
    headers: {
      ...headers,
      "Content-Type": "application/json",
      "Idempotency-Key": key,
    },
  };

  let res = http.post(`${BASE}/api/orders`, body, opts);

  // Retry with the SAME key on server errors (idempotent).
  let attempt = 0;
  while (res.status >= 500 && attempt < 10) {
    attempt++;
    const backoff = Math.min(Math.pow(2, attempt) * 100, 120000);
    sleep(backoff / 1000);
    res = http.post(`${BASE}/api/orders`, body, opts);
  }

  if (res.status === 201 || res.status === 200) {
    const j = res.json();
    console.log(
      JSON.stringify({
        event: "confirmed",
        t: Date.now(),
        key: key,
        order_id: j.id,
        status: j.status,
      })
    );
    return res;
  }
  return res;
}

function orderRead(orderId) {
  return http.get(`${BASE}/api/orders/${orderId}`, { headers });
}

// ---------------------------------------------------------------------------
// Default function — called by each VU iteration
// ---------------------------------------------------------------------------
let lastOrderId = null;

export default function () {
  const dice = Math.random();

  if (dice < 0.6) {
    // 60 % catalog reads
    const res = catalogRead();
    check(res, { "catalog 2xx": (r) => r.status >= 200 && r.status < 300 });
  } else if (dice < 0.9) {
    // 30 % order creates
    const res = orderCreate();
    check(res, {
      "order accepted": (r) => r.status === 201 || r.status === 200,
    });
    if (res.status === 201 || res.status === 200) {
      lastOrderId = res.json().id;
    }
  } else {
    // 10 % order reads
    if (lastOrderId) {
      const res = orderRead(lastOrderId);
      check(res, { "order read 2xx": (r) => r.status === 200 });
    } else {
      catalogRead(); // fallback if no order yet
    }
  }
}
