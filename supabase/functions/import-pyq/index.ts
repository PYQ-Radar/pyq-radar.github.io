// Supabase Edge Function: import-pyq
// Receives parsed PYQ records from a trusted automation runner.
// Imported records are ALWAYS unverified until reviewed in the dashboard.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-import-token",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

type ImportRecord = {
  exam_id: string;
  subject_id?: string | null;
  state_id?: string | null;
  topic_id?: string | null;
  year: number;
  paper: string;
  question_text: string;
  explanation?: string | null;
  source_name: string;
  source_url: string;
  options: { option_key: string; option_text: string }[];
  correct_option?: string | null;
  answer_explanation?: string | null;
};

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (req.method !== "POST") return response({ error: "Method not allowed" }, 405);

  const importToken = Deno.env.get("PYQ_IMPORT_TOKEN");
  const suppliedToken = req.headers.get("x-import-token");
  if (!importToken || !suppliedToken || suppliedToken !== importToken) {
    return response({ error: "Unauthorized import request" }, 401);
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceRoleKey) {
    return response({ error: "Server configuration is incomplete" }, 500);
  }

  let payload: { records?: ImportRecord[] };
  try {
    payload = await req.json();
  } catch {
    return response({ error: "Request body must be valid JSON" }, 400);
  }

  const records = payload.records;
  if (!Array.isArray(records) || records.length === 0 || records.length > 100) {
    return response({ error: "Send between 1 and 100 records per request" }, 400);
  }

  const client = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const results: { index: number; status: string; question_id?: string; error?: string }[] = [];

  for (let index = 0; index < records.length; index++) {
    const item = records[index];
    if (
      !item || !item.exam_id || !Number.isInteger(item.year) ||
      !item.paper?.trim() || !item.question_text?.trim() ||
      !item.source_name?.trim() || !item.source_url?.trim() ||
      !Array.isArray(item.options) || item.options.length < 2
    ) {
      results.push({ index, status: "rejected", error: "Missing required fields or fewer than two options" });
      continue;
    }

    const cleanOptions = item.options.filter(
      (o) => o && /^[A-F]$/.test(o.option_key) && typeof o.option_text === "string" && o.option_text.trim(),
    );
    if (cleanOptions.length !== item.options.length) {
      results.push({ index, status: "rejected", error: "Options must use keys A-F and non-empty text" });
      continue;
    }

    // Idempotency: do not insert the same question text from the same official source twice.
    const { data: existing, error: lookupError } = await client
      .from("questions")
      .select("id")
      .eq("source_url", item.source_url)
      .eq("question_text", item.question_text.trim())
      .limit(1)
      .maybeSingle();

    if (lookupError) {
      results.push({ index, status: "error", error: lookupError.message });
      continue;
    }
    if (existing?.id) {
      results.push({ index, status: "skipped_duplicate", question_id: existing.id });
      continue;
    }

    const { data: question, error: questionError } = await client
      .from("questions")
      .insert({
        exam_id: item.exam_id,
        subject_id: item.subject_id ?? null,
        state_id: item.state_id ?? null,
        topic_id: item.topic_id ?? null,
        year: item.year,
        paper: item.paper.trim(),
        question_text: item.question_text.trim(),
        explanation: item.explanation ?? null,
        source_name: item.source_name.trim(),
        source_url: item.source_url.trim(),
        is_verified: false,
      })
      .select("id")
      .single();

    if (questionError || !question?.id) {
      results.push({ index, status: "error", error: questionError?.message ?? "Question insert failed" });
      continue;
    }

    const questionId = question.id as string;
    const { error: optionsError } = await client.from("question_options").insert(
      cleanOptions.map((option) => ({
        question_id: questionId,
        option_key: option.option_key,
        option_text: option.option_text.trim(),
      })),
    );

    if (optionsError) {
      await client.from("questions").delete().eq("id", questionId);
      results.push({ index, status: "error", error: optionsError.message });
      continue;
    }

    if (item.correct_option) {
      const { error: answerError } = await client.from("question_answers").insert({
        question_id: questionId,
        correct_option: item.correct_option,
        explanation: item.answer_explanation ?? item.explanation ?? null,
      });
      if (answerError) {
        await client.from("questions").delete().eq("id", questionId);
        results.push({ index, status: "error", error: answerError.message });
        continue;
      }
    }

    results.push({ index, status: "imported_unverified", question_id: questionId });
  }

  return response({
    success: true,
    received: records.length,
    imported: results.filter((r) => r.status === "imported_unverified").length,
    skipped: results.filter((r) => r.status === "skipped_duplicate").length,
    failed: results.filter((r) => r.status === "error" || r.status === "rejected").length,
    results,
  });
});
