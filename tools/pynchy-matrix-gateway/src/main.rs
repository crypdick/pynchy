use std::{
    fs::{self, File, OpenOptions},
    io::{self, BufRead, Read, Write},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::mpsc,
    time::{Duration, Instant},
};

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use matrix_sdk::{
    authentication::matrix::MatrixSession,
    config::{RequestConfig, SyncSettings},
    ruma::{
        events::{
            key::verification::VerificationMethod,
            room::message::{MessageType, RoomMessageEventContent},
            AnySyncMessageLikeEvent, AnySyncTimelineEvent, SyncMessageLikeEvent,
        },
        OwnedDeviceId, OwnedRoomId, OwnedUserId,
    },
    store::RoomLoadSettings,
    Client,
};
use serde::{Deserialize, Serialize};

const APP_NAME: &str = "pynchy-matrix-gateway";
const DEVICE_NAME: &str = "Pynchy communications gateway";

#[derive(Parser)]
#[command(
    name = APP_NAME,
    about = "Host-only Matrix client for Pynchy communications",
    long_about = "A host-only Matrix client. It reads the owner's joined rooms and can send a message only when its caller explicitly supplies the body on standard input."
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Log in once and persist a dedicated Matrix gateway session.
    Login {
        #[arg(long)]
        homeserver: String,
        #[arg(long)]
        user: String,
        /// Read the Matrix password from standard input, never command arguments.
        #[arg(long)]
        password_stdin: bool,
    },
    /// List every room already joined by the gateway owner.
    Chats,
    /// Print recent text messages from one explicitly named room as JSON Lines.
    Messages {
        #[arg(long)]
        room: String,
        #[arg(long, default_value_t = 50, value_parser = clap::value_parser!(u32).range(1..=250))]
        limit: u32,
    },
    /// Verify this gateway device against an existing Element device using SAS.
    Verify {
        /// The existing, already-trusted Element device ID.
        #[arg(long)]
        device: String,
        /// Fail rather than wait indefinitely for the other device.
        #[arg(long, default_value_t = 600, value_parser = clap::value_parser!(u64).range(30..=1800))]
        timeout_seconds: u64,
        /// Optional local file containing `confirm` after the emoji comparison.
        #[arg(long)]
        confirmation_file: Option<PathBuf>,
    },
    /// Send one plain-text Matrix message as the gateway owner.
    Send {
        #[arg(long)]
        room: String,
        /// Read the message body from standard input, never command arguments.
        #[arg(long)]
        body_stdin: bool,
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
struct Chat {
    room_id: String,
    name: String,
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
    Ok(PathBuf::from(home).join(".local/share/pynchy/matrix-gateway"))
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
            "no Matrix gateway session at {}; run login first",
            path.display()
        )
    })?;
    serde_json::from_slice(&raw).context("parsing saved Matrix gateway session")
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

fn verification_confirmation_receiver() -> mpsc::Receiver<String> {
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        if io::stdin().lock().read_line(&mut line).is_ok() {
            let _ = sender.send(line.trim().to_owned());
        }
    });
    receiver
}

fn take_file_confirmation(path: &Path) -> Result<Option<String>> {
    match fs::read_to_string(path) {
        Ok(value) => {
            fs::remove_file(path)
                .with_context(|| format!("removing confirmation file {}", path.display()))?;
            Ok(Some(value.trim().to_owned()))
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => {
            Err(error).with_context(|| format!("reading confirmation file {}", path.display()))
        }
    }
}

async fn verify(
    device_id: String,
    timeout_seconds: u64,
    confirmation_file: Option<PathBuf>,
) -> Result<()> {
    if let Some(path) = confirmation_file.as_deref() {
        match fs::remove_file(path) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("clearing confirmation file {}", path.display()))
            }
        }
    }
    let (client, saved) = restored_client().await?;
    initial_sync(&client).await?;

    let target_device_id: OwnedDeviceId = device_id.into();
    if target_device_id.as_str() == saved.device_id {
        bail!("refusing to verify the gateway against itself");
    }
    let owner = saved
        .user_id
        .parse::<OwnedUserId>()
        .context("parsing saved Matrix user ID")?;
    let device = client
        .encryption()
        .get_device(&owner, &target_device_id)
        .await
        .context("loading target Matrix device")?
        .context("target Matrix device is unknown to the gateway")?;
    if device.is_verified() {
        println!(
            "{}",
            serde_json::json!({"status": "already_verified", "device_id": target_device_id})
        );
        return Ok(());
    }

    let request = device
        .request_verification_with_methods(vec![VerificationMethod::SasV1])
        .await
        .context("requesting Matrix device verification")?;
    let flow_id = request.flow_id().to_owned();
    println!(
        "{}",
        serde_json::json!({
            "status": "request_sent",
            "device_id": target_device_id,
            "flow_id": flow_id,
            "instruction": "Accept the verification request in Element; this command will print the seven emojis to compare."
        })
    );

    let deadline = Instant::now() + Duration::from_secs(timeout_seconds);
    let confirmation = verification_confirmation_receiver();
    let mut sas = None;
    let mut presented = false;
    let mut confirmed = false;

    while Instant::now() < deadline {
        client
            .sync_once(SyncSettings::default().timeout(Duration::from_secs(5)))
            .await
            .context("waiting for Matrix verification events")?;

        if sas.is_none() {
            sas = client
                .encryption()
                .get_verification(&owner, &flow_id)
                .await
                .and_then(|verification| verification.sas());
        }

        if sas.is_none() {
            if let Some(request) = client
                .encryption()
                .get_verification_request(&owner, &flow_id)
                .await
            {
                if request.is_cancelled() {
                    bail!("Matrix device verification was cancelled");
                }
                if request.is_ready() {
                    sas = request
                        .start_sas()
                        .await
                        .context("starting Matrix SAS verification")?;
                }
            }
        }

        if let Some(sas_verification) = sas.as_ref() {
            if sas_verification.is_cancelled() {
                bail!("Matrix SAS verification was cancelled");
            }
            if sas_verification.is_done() {
                println!(
                    "{}",
                    serde_json::json!({"status": "verified", "device_id": target_device_id})
                );
                return Ok(());
            }
            if !presented && sas_verification.can_be_presented() {
                let emojis = sas_verification
                    .emoji()
                    .context("Matrix verification did not provide emojis")?
                    .iter()
                    .map(|emoji| serde_json::json!({"symbol": emoji.symbol, "description": emoji.description}))
                    .collect::<Vec<_>>();
                println!(
                    "{}",
                    serde_json::json!({
                        "status": "compare",
                        "emojis": emojis,
                        "instruction": "Compare these seven emojis with Element. Type confirm and press Enter only if they match; otherwise terminate this command."
                    })
                );
                presented = true;
            }
            if presented && !confirmed {
                let stdin_confirmation = match confirmation.try_recv() {
                    Ok(value) => Some(value),
                    Err(mpsc::TryRecvError::Empty) => None,
                    Err(mpsc::TryRecvError::Disconnected) if confirmation_file.is_none() => {
                        bail!("verification input closed before confirmation")
                    }
                    Err(mpsc::TryRecvError::Disconnected) => None,
                };
                let file_confirmation = confirmation_file
                    .as_deref()
                    .map(take_file_confirmation)
                    .transpose()?
                    .flatten();
                match stdin_confirmation.or(file_confirmation) {
                    Some(value) if value == "confirm" => {
                        sas_verification
                            .confirm()
                            .await
                            .context("confirming Matrix SAS verification")?;
                        confirmed = true;
                    }
                    Some(_) => bail!("verification requires the exact confirmation word: confirm"),
                    None => {}
                }
            }
        }
    }

    bail!("Matrix device verification timed out")
}

fn message_from_event(room_id: &str, event: AnySyncTimelineEvent) -> Option<ReadMessage> {
    let AnySyncTimelineEvent::MessageLike(AnySyncMessageLikeEvent::RoomMessage(event)) = event
    else {
        return None;
    };
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

async fn login(homeserver: String, user: String, password_stdin: bool) -> Result<()> {
    if !password_stdin {
        bail!("refusing password arguments; pass --password-stdin and provide it on standard input");
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
    write_private(
        &session_path()?,
        &serde_json::to_vec(&saved).context("serializing Matrix session")?,
    )?;
    println!(
        "{{\"user_id\":{},\"device_id\":{},\"status\":\"logged_in\"}}",
        serde_json::to_string(&saved.user_id)?,
        serde_json::to_string(&saved.device_id)?,
    );
    Ok(())
}

async fn chats() -> Result<()> {
    let (client, _) = restored_client().await?;
    initial_sync(&client).await?;
    let mut chats = Vec::new();
    for room in client.joined_rooms() {
        let name = room
            .display_name()
            .await
            .map(|value| value.to_string())
            .unwrap_or_else(|_| room.room_id().to_string());
        chats.push(Chat {
            room_id: room.room_id().to_string(),
            name,
        });
    }
    println!("{}", serde_json::to_string(&chats)?);
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
        .context("requested room is not joined by this gateway")?;
    let mut options = matrix_sdk::room::MessagesOptions::backward();
    options.limit = limit.into();
    let response = room
        .messages(options)
        .await
        .context("loading Matrix messages")?;
    let encrypted_events = response
        .chunk
        .iter()
        .filter(|event| event.raw().get_field::<String>("type").ok().flatten().as_deref() == Some("m.room.encrypted"))
        .count();
    let mut message_count = 0;
    for event in response.chunk.into_iter().rev() {
        let raw = event.into_raw();
        if let Ok(event) = raw.deserialize() {
            if let Some(message) = message_from_event(room.room_id().as_str(), event) {
                println!("{}", serde_json::to_string(&message)?);
                message_count += 1;
            }
        }
    }
    if message_count == 0 && encrypted_events > 0 {
        bail!(
            "MATRIX_GATEWAY_E2EE_KEYS_UNAVAILABLE: this room has encrypted history but the gateway has no usable room keys; complete gateway device verification before retrying"
        );
    }
    Ok(())
}

async fn send(room_id: String, body_stdin: bool) -> Result<()> {
    if !body_stdin {
        bail!("refusing message arguments; pass --body-stdin and provide the body on standard input");
    }
    let mut body = String::new();
    io::stdin()
        .read_to_string(&mut body)
        .context("reading message body from standard input")?;
    let body = body.trim_end_matches(['\r', '\n']);
    if body.is_empty() {
        bail!("empty message body on standard input");
    }

    let (client, _) = restored_client().await?;
    initial_sync(&client).await?;
    let room_id = room_id
        .parse::<OwnedRoomId>()
        .context("parsing Matrix room ID")?;
    let room = client
        .get_room(&room_id)
        .context("requested room is not joined by this gateway")?;
    let response = room
        .send(RoomMessageEventContent::text_plain(body))
        .await
        .context("sending Matrix message")?;
    println!(
        "{}",
        serde_json::json!({"room_id": room.room_id().to_string(), "event_id": response.response.event_id.to_string()})
    );
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
        Command::Chats => chats().await,
        Command::Messages { room, limit } => messages(room, limit).await,
        Command::Verify {
            device,
            timeout_seconds,
            confirmation_file,
        } => verify(device, timeout_seconds, confirmation_file).await,
        Command::Send { room, body_stdin } => send(room, body_stdin).await,
    }
}
