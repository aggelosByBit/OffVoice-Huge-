'use strict';
const $ = id => document.getElementById(id);
let busy = true;
let failedRequest = null;

function append(text, role) {
  const node = document.createElement('div');
  node.className = `message ${role}`;
  node.textContent = text;
  $('messages').appendChild(node);
  $('messages').scrollTop = $('messages').scrollHeight;
}

async function api(path, payload) {
  const response = await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Δεν έγινε δεκτό το αίτημα.');
  return data;
}

function setBusy(value) {
  busy = value;
  $('send').disabled = $('reset').disabled = $('callback').disabled = value;
  $('message').disabled = value;
}

async function send(payload, display = true) {
  if (busy) return;
  if (display) append(payload.message, 'user');
  setBusy(true);
  $('retry').hidden = true;
  $('status').textContent = 'Το αίτημά σας επεξεργάζεται…';
  try {
    const data = await api('/chat', payload);
    append(data.response, 'bot');
    failedRequest = null;
    $('status').textContent = '';
  } catch (error) {
    failedRequest = payload;
    $('status').textContent = `${error.message} Μπορείτε να επαναλάβετε το ίδιο αίτημα.`;
    $('retry').hidden = false;
  } finally {
    setBusy(false);
    $('message').focus();
  }
}

$('chatForm').addEventListener('submit', event => {
  event.preventDefault();
  const message = $('message').value.trim();
  if (!message || busy) return;
  $('message').value = '';
  send({message, request_id: crypto.randomUUID(), callback_allowed: $('callback').checked});
});
$('retry').addEventListener('click', () => failedRequest && send(failedRequest, false));
$('reset').addEventListener('click', async () => {
  if (busy) return;
  setBusy(true);
  try {
    await api('/reset', {});
    $('messages').replaceChildren();
    $('callback').checked = false;
    failedRequest = null;
    $('retry').hidden = true;
    $('status').textContent = '';
    append('Η συνομιλία διαγράφηκε. Πώς μπορώ να σας εξυπηρετήσω;', 'bot');
  } catch (error) {
    $('status').textContent = 'Η διαγραφή δεν επιβεβαιώθηκε. Παρακαλώ δοκιμάστε ξανά.';
  } finally { setBusy(false); }
});
$('callback').addEventListener('change', async () => {
  const wanted = $('callback').checked;
  try { await api('/callback-consent', {callback_allowed: wanted}); }
  catch (error) {
    $('callback').checked = !wanted;
    $('status').textContent = 'Η επιλογή επανάκλησης δεν ενημερώθηκε. Παρακαλώ δοκιμάστε ξανά.';
  }
});

(async () => {
  try {
    const response = await fetch('/config', {cache: 'no-store'});
    if (!response.ok) throw new Error();
    const config = await response.json();
    $('clinic').textContent = config.clinic_name + (config.demo ? ' · Demo' : '');
    $('privacySummary').textContent = config.privacy_summary;
    $('privacyNotice').textContent = config.privacy_notice;
    $('retention').textContent = `Το ιστορικό παραμένει προσωρινά στον server και διαγράφεται αυτόματα μετά από περίπου ${config.session_minutes} λεπτά αδράνειας. Δεν επανεμφανίζεται μετά από ανανέωση της σελίδας.`;
    $('callbackInfo').textContent = `Αν επιλεγεί, όνομα και τηλέφωνο που θα δώσετε μπορούν να σταλούν με email στο ιατρείο μετά από περίπου ${config.abandoned_minutes} λεπτά αδράνειας. Μην γράφετε αναλυτικό ιατρικό ιστορικό.`;
    $('message').maxLength = config.max_message_chars;
    append('Γεια σας! Είμαι η ψηφιακή γραμματεία. Πώς μπορώ να σας εξυπηρετήσω;', 'bot');
    setBusy(false);
  } catch (error) { $('status').textContent = 'Αδυναμία φόρτωσης. Παρακαλώ ανανεώστε τη σελίδα.'; }
})();
