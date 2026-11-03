import {
  Action,
  ActionPanel,
  Detail,
  environment,
  getPreferenceValues,
  Grid,
  Icon,
  showToast,
  Toast,
} from "@raycast/api";
import { useFetch } from "@raycast/utils";
import { mkdir, readdir, rm, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { useEffect, useState } from "react";

type Moment = {
  kind: "seen" | "said" | "on_screen";
  time: number;
  time_label: string;
  snippet?: string;
  frame_url?: string | null;
};

type Photo = {
  uuid: string;
  filename: string;
  date: string;
  persons: string[];
  place: string;
  media_type: "image" | "video";
  score: number | null;
  thumbnail_url: string;
  preview_url: string;
  moment?: Moment;
};

const kinds = { seen: "seen", said: "said", on_screen: "on screen" };

function momentLabel(moment: Moment) {
  return `${moment.time_label} · ${kinds[moment.kind] ?? moment.kind}`;
}

type SearchResponse = { results: Photo[]; total: number };

const service = (getPreferenceValues<{ serviceUrl?: string }>().serviceUrl || "http://127.0.0.1:18473").replace(
  /\/+$/,
  "",
);

// The service refuses cross-site image loads, so previews are fetched here and shown from a private cache.
const cache = join(environment.supportPath, "previews");
const cacheLimit = 600;
const inflight = new Map<string, Promise<string | undefined>>();

function imageUrl(photo: Photo, kind: "thumbnail" | "preview") {
  // A video moment shows its matching frame; Photos itself cannot jump to that time.
  if (photo.moment?.frame_url) return photo.moment.frame_url;
  return kind === "thumbnail" ? photo.thumbnail_url : photo.preview_url;
}

async function cachedImage(photo: Photo, kind: "thumbnail" | "preview") {
  const url = imageUrl(photo, kind);
  const frame = url.match(/\/frames\/(\d+)$/)?.[1];
  const file = join(cache, frame ? `${photo.uuid}-frame-${frame}.jpg` : `${photo.uuid}-${kind}.jpg`);
  const key = file;
  if (!inflight.has(key)) {
    inflight.set(
      key,
      (async () => {
        try {
          await stat(file);
          return file;
        } catch {
          const response = await fetch(`${service}${url}`);
          if (!response.ok) return undefined;
          await mkdir(cache, { recursive: true, mode: 0o700 });
          await writeFile(file, Buffer.from(await response.arrayBuffer()), { mode: 0o600 });
          return file;
        }
      })().catch(() => {
        inflight.delete(key);
        return undefined;
      }),
    );
  }
  return inflight.get(key);
}

async function pruneCache() {
  try {
    const files = await Promise.all(
      (await readdir(cache)).map(async (name) => ({ name, time: (await stat(join(cache, name))).mtimeMs })),
    );
    files.sort((a, b) => b.time - a.time);
    await Promise.all(files.slice(cacheLimit).map(({ name }) => rm(join(cache, name), { force: true })));
  } catch {
    // An absent cache needs no pruning.
  }
}

function useImages(photos: Photo[], kind: "thumbnail" | "preview") {
  const [paths, setPaths] = useState<Record<string, string>>({});
  useEffect(() => {
    let live = true;
    photos.forEach((photo) =>
      cachedImage(photo, kind).then((path) => {
        if (live && path) setPaths((current) => ({ ...current, [photo.uuid]: path }));
      }),
    );
    return () => {
      live = false;
    };
  }, [photos.map((photo) => `${photo.uuid}${photo.moment?.frame_url ?? ""}`).join(), kind]);
  return paths;
}

function caption(photo: Photo) {
  return [photo.date.slice(0, 10), photo.place].filter(Boolean).join(" · ");
}

async function openInPhotos(photo: Photo) {
  const toast = await showToast({ style: Toast.Style.Animated, title: "Opening in Photos" });
  try {
    const time = photo.moment ? `?t=${encodeURIComponent(photo.moment.time)}` : "";
    const response = await fetch(`${service}/api/assets/${photo.uuid}/open${time}`, {
      method: "POST",
      headers: { "X-Media-Search-Client": "cli" },
    });
    if (!response.ok)
      throw new Error(((await response.json()) as { detail?: string }).detail || `HTTP ${response.status}`);
    toast.style = Toast.Style.Success;
    toast.title = "Opened in Photos";
    if (photo.moment) toast.message = `Scrub to ${photo.moment.time_label}`;
  } catch (error) {
    toast.style = Toast.Style.Failure;
    toast.title = "Photos could not open it";
    toast.message = error instanceof Error ? error.message : String(error);
  }
}

function PhotoDetail({ photo }: { photo: Photo }) {
  const path = useImages([photo], "preview")[photo.uuid];
  return (
    <Detail
      isLoading={!path}
      navigationTitle={caption(photo)}
      markdown={path ? `![${photo.filename}](${encodeURI(`file://${path}`)})` : ""}
      metadata={
        <Detail.Metadata>
          <Detail.Metadata.Label title="Date" text={photo.date.slice(0, 16).replace("T", " ")} />
          {photo.place ? <Detail.Metadata.Label title="Place" text={photo.place} /> : null}
          {photo.persons.length ? <Detail.Metadata.Label title="People" text={photo.persons.join(", ")} /> : null}
          <Detail.Metadata.Label title="File" text={photo.filename} />
          {photo.moment ? <Detail.Metadata.Label title="Moment" text={momentLabel(photo.moment)} /> : null}
          {photo.moment?.snippet ? <Detail.Metadata.Label title="Words" text={`“${photo.moment.snippet}”`} /> : null}
        </Detail.Metadata>
      }
      actions={<Actions photo={photo} />}
    />
  );
}

function Actions({ photo, detail }: { photo: Photo; detail?: boolean }) {
  return (
    <ActionPanel>
      <Action title="Open in Photos" icon={Icon.Image} onAction={() => openInPhotos(photo)} />
      {detail ? <Action.Push title="Show Preview" icon={Icon.Eye} target={<PhotoDetail photo={photo} />} /> : null}
      <Action.CopyToClipboard title="Copy Photo ID" content={photo.uuid} shortcut={{ modifiers: ["cmd"], key: "c" }} />
      <Action.OpenInBrowser
        title="Open Findstill Dashboard"
        url={service}
        shortcut={{ modifiers: ["cmd"], key: "d" }}
      />
    </ActionPanel>
  );
}

export default function Command() {
  const [query, setQuery] = useState("");
  const [mediaType, setMediaType] = useState("");
  const params = new URLSearchParams({ q: query.trim(), limit: "60", media_type: mediaType });
  useEffect(() => {
    pruneCache();
  }, []);
  const { data, isLoading, error } = useFetch<SearchResponse>(`${service}/api/search?${params}`, {
    keepPreviousData: true,
  });

  const thumbnails = useImages(data?.results ?? [], "thumbnail");

  return (
    <Grid
      columns={5}
      aspectRatio="1"
      fit={Grid.Fit.Fill}
      isLoading={isLoading}
      throttle
      searchText={query}
      onSearchTextChange={setQuery}
      searchBarPlaceholder="Describe a photo, or words said or shown in a video"
      searchBarAccessory={
        <Grid.Dropdown tooltip="Media type" onChange={setMediaType} storeValue>
          <Grid.Dropdown.Item title="Photos and Videos" value="" />
          <Grid.Dropdown.Item title="Photos" value="image" />
          <Grid.Dropdown.Item title="Videos" value="video" />
        </Grid.Dropdown>
      }
    >
      {error ? (
        <Grid.EmptyView
          icon={Icon.ExclamationMark}
          title="Findstill is not running"
          description={`Start it with \`findstill serve\`, or check the service address in preferences. (${error.message})`}
        />
      ) : (
        <Grid.Section
          title={query.trim() ? "Best matches" : "Most recent"}
          subtitle={data ? `${data.total.toLocaleString()} searchable` : undefined}
        >
          {(data?.results ?? []).map((photo) => (
            <Grid.Item
              key={photo.uuid}
              content={thumbnails[photo.uuid] ? { source: thumbnails[photo.uuid] } : ""}
              title={caption(photo)}
              subtitle={
                photo.media_type === "video"
                  ? photo.moment
                    ? `Video · ${momentLabel(photo.moment)}`
                    : "Video"
                  : photo.persons.join(", ")
              }
              actions={<Actions photo={photo} detail />}
            />
          ))}
        </Grid.Section>
      )}
    </Grid>
  );
}
