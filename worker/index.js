// Cloudflare Worker: triggers GitHub Actions workflow to refresh SPX data
// The GITHUB_TOKEN secret is set via `wrangler secret put GITHUB_TOKEN`

addEventListener("fetch", (event) => {
  event.respondWith(handleRequest(event.request));
});

async function handleRequest(request) {
  // CORS + method check
  if (request.method === "OPTIONS") {
    return new Response(null, {
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      },
    });
  }

  if (request.method !== "POST") {
    return jsonResponse({ error: "Method not allowed" }, 405);
  }

  const token = typeof GITHUB_TOKEN !== "undefined" ? GITHUB_TOKEN : null;
  if (!token) {
    return jsonResponse({ error: "Worker not configured: missing GITHUB_TOKEN" }, 500);
  }

  const repo = typeof GITHUB_REPO !== "undefined" ? GITHUB_REPO : "HenrikSondergaard/3-percent-strategy";
  const workflowId = "update-data.yml";
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${workflowId}/dispatches`;

  // Parse optional expiration from request body
  let body = {};
  try { body = await request.json(); } catch (e) {}
  const expiration = body.expiration || "";

  const inputs = {};
  if (expiration) {
    inputs.expiration = expiration;
  } else {
    inputs.expirations = "20";
  }

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Accept": "application/vnd.github+json",
        "User-Agent": "spx-refresh-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: inputs,
      }),
    });

    if (resp.status === 204) {
      const msg = expiration
        ? `Workflow dispatched for ${expiration}. Data will update in ~1 minute.`
        : "Workflow dispatched. Data will update in ~2-3 minutes.";
      return jsonResponse({ status: "triggered", expiration: expiration || null, message: msg });
    }

    // Rate limited or other error
    const body = await resp.text();
    return jsonResponse({ error: `GitHub API returned ${resp.status}`, details: body }, 502);
  } catch (e) {
    return jsonResponse({ error: e.message }, 500);
  }
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
