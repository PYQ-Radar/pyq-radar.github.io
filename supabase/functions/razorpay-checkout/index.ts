import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.0";

const cors = {
  "Access-Control-Allow-Origin": "https://pyq-radar.github.io",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Content-Type": "application/json"
};
const prices: Record<string, Record<string, number>> = {
  Basic: { Monthly: 9, "6 Months": 49, Yearly: 89 },
  Premium: { Monthly: 29, "6 Months": 149, Yearly: 299 },
  Pro: { Monthly: 59, "6 Months": 299, Yearly: 599 }
};
const days: Record<string, number> = { Monthly: 30, "6 Months": 182, Yearly: 365 };
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: cors });

function envJson(name: string) {
  try { return JSON.parse(Deno.env.get(name) || "{}"); } catch { return {}; }
}
function hex(buffer: ArrayBuffer) {
  return [...new Uint8Array(buffer)].map(b => b.toString(16).padStart(2, "0")).join("");
}
async function hmac(message: string, secret: string) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  return hex(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message)));
}
function safeEqual(a: string, b: string) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
function addDays(base: Date, count: number) {
  const result = new Date(base);
  result.setUTCDate(result.getUTCDate() + count);
  return result;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "Method not allowed" }, 405);

  const authHeader = req.headers.get("Authorization") || "";
  if (!authHeader.startsWith("Bearer ")) return json({ error: "Please sign in first." }, 401);

  const url = Deno.env.get("SUPABASE_URL")!;
  const publishable = envJson("SUPABASE_PUBLISHABLE_KEYS").default || Deno.env.get("SUPABASE_ANON_KEY");
  const secretKey = envJson("SUPABASE_SECRET_KEYS").default || Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const keyId = Deno.env.get("RAZORPAY_KEY_ID");
  const keySecret = Deno.env.get("RAZORPAY_KEY_SECRET");
  if (!url || !publishable || !secretKey) return json({ error: "Payment service is not configured yet." }, 503);
  if (!keyId || !keySecret) return json({ error: "Razorpay merchant credentials are not configured yet. No payment has been taken." }, 503);

  const userClient = createClient(url, publishable, {
    global: { headers: { Authorization: authHeader } },
    auth: { persistSession: false, autoRefreshToken: false }
  });
  const token = authHeader.slice(7);
  const { data: userResult, error: userError } = await userClient.auth.getUser(token);
  const user = userResult?.user;
  if (userError || !user) return json({ error: "Your session is invalid. Please sign in again." }, 401);

  const admin = createClient(url, secretKey, { auth: { persistSession: false, autoRefreshToken: false } });
  let body: any;
  try { body = await req.json(); } catch { return json({ error: "Invalid request." }, 400); }

  if (body.action === "create_order") {
    const plan = body.plan;
    const duration = body.duration;
    const amount = prices[plan]?.[duration];
    if (!amount) return json({ error: "Invalid plan or billing period." }, 400);

    const receipt = "pqr_" + crypto.randomUUID().replaceAll("-", "").slice(0, 24);
    const rp = await fetch("https://api.razorpay.com/v1/orders", {
      method: "POST",
      headers: {
        "Authorization": "Basic " + btoa(keyId + ":" + keySecret),
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        amount: amount * 100,
        currency: "INR",
        receipt,
        notes: { user_id: user.id, plan, duration, product: "PYQ Radar" }
      })
    });
    const order = await rp.json();
    if (!rp.ok || !order.id) {
      console.error("Razorpay order creation failed", order?.error?.code || rp.status);
      return json({ error: "Could not create a payment order. Please try again later." }, 502);
    }

    const { error: insertError } = await admin.from("payment_orders").insert({
      user_id: user.id,
      plan,
      duration,
      amount_paise: amount * 100,
      currency: "INR",
      razorpay_order_id: order.id,
      status: "created"
    });
    if (insertError) {
      console.error("Payment order persistence failed", insertError.message);
      return json({ error: "Could not save the order securely. Please contact support." }, 500);
    }
    return json({
      key_id: keyId,
      order_id: order.id,
      amount: amount * 100,
      currency: "INR",
      name: "PYQ Radar",
      description: plan + " Plan · " + duration
    });
  }

  if (body.action === "verify_payment") {
    const orderId = String(body.razorpay_order_id || "");
    const paymentId = String(body.razorpay_payment_id || "");
    const signature = String(body.razorpay_signature || "");
    if (!orderId || !paymentId || !/^[a-f0-9]{64}$/i.test(signature)) {
      return json({ error: "Payment verification details are incomplete." }, 400);
    }

    const { data: record, error: recordError } = await admin.from("payment_orders")
      .select("*").eq("razorpay_order_id", orderId).eq("user_id", user.id).maybeSingle();
    if (recordError || !record) return json({ error: "This payment order was not found for your account." }, 404);

    const expected = await hmac(orderId + "|" + paymentId, keySecret);
    if (!safeEqual(expected.toLowerCase(), signature.toLowerCase())) {
      return json({ error: "Payment signature verification failed." }, 400);
    }

    const paymentResponse = await fetch("https://api.razorpay.com/v1/payments/" + encodeURIComponent(paymentId), {
      headers: { "Authorization": "Basic " + btoa(keyId + ":" + keySecret) }
    });
    const payment = await paymentResponse.json();
    if (!paymentResponse.ok || payment.order_id !== orderId ||
        payment.amount !== record.amount_paise || payment.currency !== "INR" ||
        payment.status !== "captured") {
      return json({ error: "Payment is not confirmed as captured yet. Your plan has not been activated." }, 409);
    }

    let expiresAt: string;
    if (record.status === "paid") {
      if (!record.expires_at || record.razorpay_payment_id !== paymentId) {
        return json({ error: "This order was already processed with a different payment reference." }, 409);
      }
      expiresAt = record.expires_at;
    } else {
      const paidAt = new Date();
      const expiryBase = user.app_metadata?.subscription_expires_at &&
        new Date(user.app_metadata.subscription_expires_at) > paidAt
          ? new Date(user.app_metadata.subscription_expires_at) : paidAt;
      expiresAt = addDays(expiryBase, days[record.duration]).toISOString();
      const { error: updateOrderError } = await admin.from("payment_orders").update({
        status: "paid",
        razorpay_payment_id: paymentId,
        paid_at: paidAt.toISOString(),
        expires_at: expiresAt
      }).eq("id", record.id).eq("status", "created");
      if (updateOrderError) {
        console.error("Order status update failed", updateOrderError.message);
        return json({ error: "Payment is verified but activation needs a retry. Contact support with your payment ID." }, 500);
      }
    }

    const appMeta = {
      ...(user.app_metadata || {}),
      subscription_plan: record.plan,
      plan: record.plan,
      subscription_status: "active",
      subscription_expires_at: expiresAt
    };
    const { error: activationError } = await admin.auth.admin.updateUserById(user.id, { app_metadata: appMeta });
    if (activationError) {
      console.error("Subscription activation failed", activationError.message);
      return json({ error: "Payment is verified but plan activation needs support. Keep your payment ID." }, 500);
    }
    return json({ success: true, plan: record.plan, expires_at: expiresAt });
  }

  return json({ error: "Unknown payment action." }, 400);
});
