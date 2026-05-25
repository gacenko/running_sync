export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Strava verification
    if (request.method === "GET") {

      const verifyToken =
        url.searchParams.get("hub.verify_token");

      if (
        verifyToken &&
        verifyToken !== "running-sync-secret"
      ) {
        return new Response(
          "Unauthorized",
          { status: 403 }
        );
      }

      return Response.json({
        "hub.challenge":
          url.searchParams.get("hub.challenge")
      });
    }

    // Strava event
    if (request.method === "POST") {

      const body = await request.json();

      console.log(body);

      // тільки нові активності
      if (
        body.object_type !== "activity" ||
        body.aspect_type !== "create"
      ) {
        return new Response("ignored");
      }

      // запуск GitHub workflow
      const response = await fetch(
        `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/sync.yml/dispatches`,
        {
          method: "POST",
          headers: {
            "Authorization":
              `Bearer ${env.GH_TOKEN}`,
            "Accept":
              "application/vnd.github+json",
            "Content-Type":
              "application/json"
          },
          body: JSON.stringify({
            ref: "main",
            inputs: {
              activity_id:
                String(body.object_id)
            }
          })
        }
      );

      console.log(
        "GitHub status:",
        response.status
      );

      return new Response("ok");
    }

    return new Response(
      "Not found",
      { status:404 }
    );
  }
}