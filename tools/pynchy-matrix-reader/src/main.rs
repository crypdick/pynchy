use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use matrix_sdk::{
    authentication::matrix::MatrixSession,
    config::{RequestConfig, SyncSettings},
    ruma::{
        events::{
            room::message::{MessageType, SyncRoomMessageEvent},
            AnySyncMessageLikeEvent, AnySyncTimelineEvent, SyncMessageLikeEvent,
        },
        OwnedRoomId, OwnedUserId,
    },
    store::RoomLoadSettings,
    Client, Room,
};
use serde::{Deserialize, Serialize};

const APP_NAME: &str = "pynchy-matrix-reader";
const DEVICE_NAME: &str = "Pynchy Matrix reader";

#[derive(Parser)]
#[command(
    name = APP_NAME,
    about = "Read-only Matrix client for Pynchy agents",
    long_about = "A read-only Matrix client. It intentionally has no command that can send, edit, react to, join, or leave rooms."
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Log in once and persist a dedicated Matrix device session.
    Login {
        #[arg(long)]
        homeserver: String,
        #[arg(long)]
        user: String,
        /// Read the Matrix password from standard input, never command arguments.
        #[arg(long)]
        password_stdin: bool,
    },
    /// List the rooms this dedicated device has already joined.
    Rooms,
    /// Print recent text messages from one explicitly named room as JSON Lines.
    Messages {
        #[arg(long)]
        room: String,
        #[arg(long, default_value_t = 50, value_parser = clap::value_parser!(u32).range(1..=250))]
        limit: u32,
    },
    /// Stream new text messages from one explicitly named room as JSON Lines.
    Tail {
        #[arg(long)]
        room: String,
    },
}

#[derive(Clone, Deserialize, Serialize)]
struct SavedSession {
    homeserver: String,
    user_id: String,
    device_id: String,
    access_token: String,
    refresh_token: Option<String>,
}

#[derive(Serialize)]
struct ReadMessage {
    room_id: String,
    event_id: String,
    sender: String,
    origin_server_ts: u64,
    body: String,
}

fn app_dir() -> Result<PathBuf> {
    let home = std::env::var_os("HOME").context("HOME is not set")?;
    Ok(PathBuf::from(home).join(".local/share/pynchy/matrix-reader"))
}

fn session_path() -> Result<PathBuf> {
    Ok(app_dir()?.join("session.json"))
}

fn store_path() -> Result<PathBuf> {
    Ok(app_dir()?.join("store"))
}

fn store_key_path() -> Result<PathBuf> {
    Ok(app_dir()?.join("store.key"))
}

fn ensure_private_dir(path: &Path) -> Result<()> {
    fs::create_dir_all(path).with_context(|| format!("creating {}", path.display()))?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .with_context(|| format!("protecting {}", path.display()))?;
    Ok(())
}

fn write_private(path: &Path, contents: &[u8]) -> Result<()> {
    let parent = path.parent().context("private file has no parent")?;
    ensure_private_dir(parent)?;
    let temporary = path.with_extension("tmp");
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&temporary)
        .with_context(|| format!("opening {}", temporary.display()))?;
    file.write_all(contents)
        .with_context(|| format!("writing {}", temporary.display()))?;
    file.sync_all()
        .with_context(|| format!("syncing {}", temporary.display()))?;
    fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))?;
    fs::rename(&temporary, path).with_context(|| format!("saving {}", path.display()))?;
    Ok(())
}

fn load_or_create_store_key() -> Result<String> {
    let path = store_key_path()?;
    if path.exists() {
        let key =
            fs::read_to_string(&path).with_context(|| format!("reading {}", path.display()))?;
        if key.trim().len() != 64 {
            bail!("Matrix store key at {} is invalid", path.display());
        }
        return Ok(key.trim().to_owned());
    }

    let mut bytes = [0_u8; 32];
    File::open("/dev/urandom")
        .context("opening system random source")?
        .read_exact(&mut bytes)
        .context("reading system random source")?;
    let key = bytes
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    write_private(&path, key.as_bytes())?;
    Ok(key)
}

fn load_session() -> Result<SavedSession> {
    let path = session_path()?;
    let raw = fs::read(&path).with_context(|| {
        format!(
            "no Matrix reader session at {}; run login first",
            path.display()
        )
    })?;
    serde_json::from_slice(&raw).context("parsing saved Matrix reader session")
}

fn matrix_session(session: &SavedSession) -> Result<MatrixSession> {
    Ok(MatrixSession {
        meta: matrix_sdk::SessionMeta {
            user_id: session
                .user_id
                .parse::<OwnedUserId>()
                .context("parsing saved Matrix user ID")?,
            device_id: session.device_id.clone().into(),
        },
        tokens: matrix_sdk::SessionTokens {
            access_token: session.access_token.clone(),
            refresh_token: session.refresh_token.clone(),
        },
    })
}

async fn build_client(homeserver: &str) -> Result<Client> {
    let root = app_dir()?;
    ensure_private_dir(&root)?;
    let store = store_path()?;
    ensure_private_dir(&store)?;
    let store_key = load_or_create_store_key()?;
    Client::builder()
        .homeserver_url(homeserver)
        .request_config(RequestConfig::new().timeout(std::time::Duration::from_secs(45)))
        .sqlite_store(store, Some(&store_key))
        .build()
        .await
        .context("creating Matrix client")
}

async fn restored_client() -> Result<(Client, SavedSession)> {
    let saved = load_session()?;
    let client = build_client(&saved.homeserver).await?;
    client
        .matrix_auth()
        .restore_session(matrix_session(&saved)?, RoomLoadSettings::default())
        .await
        .context("restoring saved Matrix session")?;
    Ok((client, saved))
}

async fn initial_sync(client: &Client) -> Result<()> {
    client
        .sync_once(SyncSettings::default().timeout(std::time::Duration::from_secs(5)))
        .await
        .context("synchronizing Matrix state")?;
    Ok(())
}

fn message_from_event(room_id: &str, event: AnySyncTimelineEvent) -> Option<ReadMessage> {
    let AnySyncTimelineEvent::MessageLike(AnySyncMessageLikeEvent::RoomMessage(event)) = event
    else {
        return None;
    };
    message_from_room_event(room_id, event)
}

fn message_from_room_event(room_id: &str, event: SyncRoomMessageEvent) -> Option<ReadMessage> {
    let SyncMessageLikeEvent::Original(event) = event else {
        return None;
    };

    let body = match event.content.msgtype {
        MessageType::Text(content) => content.body,
        MessageType::Notice(content) => content.body,
        MessageType::Emote(content) => content.body,
        _ => return None,
    };

    Some(ReadMessage {
        room_id: room_id.to_owned(),
        event_id: event.event_id.to_string(),
        sender: event.sender.to_string(),
        origin_server_ts: event.origin_server_ts.0.into(),
        body,
    })
}

fn print_message(message: ReadMessage) {
    println!(
        "{}",
        serde_json::to_string(&message).expect("message output is serializable")
    );
}

async fn login(homeserver: String, user: String, password_stdin: bool) -> Result<()> {
    if !password_stdin {
        bail!(
            "refusing password arguments; pass --password-stdin and provide it on standard input"
        );
    }
    let mut password = String::new();
    io::stdin()
        .read_to_string(&mut password)
        .context("reading Matrix password from standard input")?;
    let password = password.trim_end_matches(['\r', '\n']);
    if password.is_empty() {
        bail!("empty Matrix password on standard input");
    }

    let client = build_client(&homeserver).await?;
    client
        .matrix_auth()
        .login_username(&user, password)
        .initial_device_display_name(DEVICE_NAME)
        .send()
        .await
        .context("logging in to Matrix")?;
    initial_sync(&client).await?;

    let session = client
        .matrix_auth()
        .session()
        .context("Matrix login did not create a session")?;
    let saved = SavedSession {
        homeserver,
        user_id: session.meta.user_id.to_string(),
        device_id: session.meta.device_id.to_string(),
        access_token: session.tokens.access_token,
        refresh_token: session.tokens.refresh_token,
    };
    let encoded = serde_json::to_vec(&saved).context("serializing Matrix session")?;
    write_private(&session_path()?, &encoded)?;
    println!(
        "{{\"user_id\":{},\"device_id\":{},\"status\":\"logged_in\"}}",
        serde_json::to_string(&saved.user_id)?,
        serde_json::to_string(&saved.device_id)?
    );
    Ok(())
}

async fn rooms() -> Result<()> {
    let (client, _) = restored_client().await?;
    initial_sync(&client).await?;
    let rooms = client
        .joined_rooms()
        .into_iter()
        .map(|room| room.room_id().to_string())
        .collect::<Vec<_>>();
    println!("{}", serde_json::to_string(&rooms)?);
    Ok(())
}

async fn messages(room_id: String, limit: u32) -> Result<()> {
    let (client, _) = restored_client().await?;
    initial_sync(&client).await?;
    let room_id = room_id
        .parse::<OwnedRoomId>()
        .context("parsing Matrix room ID")?;
    let room = client
        .get_room(&room_id)
        .context("requested room is not joined by this device")?;
    let mut options = matrix_sdk::room::MessagesOptions::backward();
    options.limit = limit.into();
    let response = room
        .messages(options)
        .await
        .context("loading Matrix messages")?;
    for event in response.chunk.into_iter().rev() {
        let raw = event.into_raw();
        if let Ok(event) = raw.deserialize() {
            if let Some(message) = message_from_event(room.room_id().as_str(), event) {
                print_message(message);
            }
        }
    }
    Ok(())
}

async fn tail(room_id: String) -> Result<()> {
    let (client, saved) = restored_client().await?;
    initial_sync(&client).await?;
    let expected_room = room_id.clone();
    let own_user = saved.user_id;
    client.add_event_handler(move |event: SyncRoomMessageEvent, room: Room| {
        let expected_room = expected_room.clone();
        let own_user = own_user.clone();
        async move {
            if room.room_id().as_str() != expected_room {
                return;
            }
            if let Some(message) = message_from_room_event(room.room_id().as_str(), event) {
                if message.sender != own_user {
                    print_message(message);
                }
            }
        }
    });
    client
        .sync(SyncSettings::default())
        .await
        .context("tailing Matrix room")?;
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    match Cli::parse().command {
        Command::Login {
            homeserver,
            user,
            password_stdin,
        } => login(homeserver, user, password_stdin).await,
        Command::Rooms => rooms().await,
        Command::Messages { room, limit } => messages(room, limit).await,
        Command::Tail { room } => tail(room).await,
    }
}
