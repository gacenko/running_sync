export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Strava webhook verification
    if (request.method === "GET") {
      return Response.json({
        "hub.challenge": url.searchParams.get("hub.challenge"),
      });
    }

    // New activity event
    if (request.method === "POST") {
      const body = await request.json();

      console.log("WORKER_VERSION_2");
      console.log("BODY:", body);

      // тільки нові активності
      if (body.object_type !== "activity" || body.aspect_type !== "create") {
        console.log("IGNORED EVENT");
        return new Response("ignored");
      }

      console.log("OWNER:", env.GITHUB_OWNER);
      console.log("REPO:", env.GITHUB_REPO);
      console.log("TOKEN EXISTS:", !!env.GH_TOKEN);

      // Відповідаємо Strava одразу — вона чекає max 2 секунди
      const githubRequest = fetch(
        `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/sync.yml/dispatches`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${env.GH_TOKEN}`,
            Accept: "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "running-sync-worker",
          },
          body: JSON.stringify({
            ref: "main",
            inputs: { activity_id: String(body.object_id) },
          }),
        }
      ).then(async (response) => {
        console.log("GitHub status:", response.status);
        console.log("GitHub response:", await response.text());
      }).catch((error) => {
        console.log("ERROR:", error.message);
      });

      // waitUntil дозволяє Workers завершити fetch після відповіді клієнту
      ctx.waitUntil(githubRequest);

      return new Response("ok", { status: 200 });
    }

    return new Response("Not found", { status: 404 });
  },
};
