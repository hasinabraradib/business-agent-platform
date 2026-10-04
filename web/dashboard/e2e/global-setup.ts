/** Seed the demo restaurant with realistic activity through the real API (offline model):
 * answered questions, small talk, knowledge gaps, a handoff with a team reply; plus bookings,
 * leads and a Telegram conversation as rows (the offline model doesn't take bookings). */
import { api, chat, createKey, saveState, sql } from "./stack";

export default async function globalSetup() {
  const key = createKey("demo-restaurant");
  saveState({ key });
  const already = await api<unknown[]>(key, "/conversations?limit=5");
  if (already.length) return; // seeded on an earlier run against the same stack

  const kacchi = await chat(key, "web-rahim", "How much is the Kacchi Biryani?");
  await chat(key, "web-rahim", "thanks!", kacchi.conversation_id);
  await chat(key, "web-sadia", "hi");
  await chat(key, "web-sadia", "Is there parking near the restaurant?");
  await chat(key, "web-tanvir", "Do you have pizza?");
  await chat(key, "web-nabila", "Do you sell pizza?");
  await chat(key, "web-imran", "What's the wifi password?");
  const handoff = await chat(key, "web-farhana", "I want to talk to a real person");
  await api(key, `/conversations/${handoff.conversation_id}/messages`, {
    method: "POST",
    body: JSON.stringify({ text: "Hi, this is Rumana from Nodi Kitchen. How can I help?" }),
  });
  await chat(key, "web-karim", "your food was cold and nobody answered my call, very upset");

  const tenant = sql("SELECT id FROM tenants WHERE slug = 'demo-restaurant'").trim();
  sql(`
    WITH c AS (
      INSERT INTO conversations (tenant_id, visitor_id, channel, status, customer_name)
      VALUES ('${tenant}', 'tg:550001', 'telegram', 'ai', 'Ayesha Rahman') RETURNING id
    ), u AS (
      INSERT INTO messages (tenant_id, conversation_id, role, content)
      SELECT '${tenant}', id, 'user', 'apnara ki friday te khola?' FROM c RETURNING conversation_id
    )
    INSERT INTO messages (tenant_id, conversation_id, role, content, outcome, model, timings)
    SELECT '${tenant}', conversation_id, 'assistant', 'Ji, Friday dupur 2:30 theke raat 11 ta porjonto khola.', 'answered', 'fake:fake-chat', '{"first_token": 640, "total": 700}'::jsonb FROM u;
  `);
  sql(`
    INSERT INTO reservations (tenant_id, conversation_id, reference, starts_at, local_date, local_time, party_size, name, phone, notes, status)
    VALUES
      ('${tenant}', '${kacchi.conversation_id}', 'R-7KQ2MX', now() + interval '1 day', (now() + interval '1 day')::date, '20:00', 4, 'Rahim Uddin', '01700000111', '', 'confirmed'),
      ('${tenant}', NULL, 'R-H3WD9P', now() + interval '3 days', (now() + interval '3 days')::date, '13:30', 2, 'Sadia Islam', '01700000222', 'Window seat if possible', 'confirmed');
    INSERT INTO leads (tenant_id, conversation_id, name, contact, interest, status)
    VALUES ('${tenant}', '${handoff.conversation_id}', 'Farhana Akter', '01700000333', 'Birthday dinner for 18 people in the private room', 'new');
  `);
}
