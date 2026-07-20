use std::{collections::BTreeSet, io::Read, time::Duration};

use anyhow::{bail, Context, Result};
use matrix_sdk::{
    config::{SyncSettings, SyncToken},
    deserialized_responses::{RawAnySyncOrStrippedState, TimelineEvent, TimelineEventKind},
    ruma::{
        api::client::{
            filter::{Filter as EventFilter, FilterDefinition, RoomEventFilter, RoomFilter},
            sync::sync_events::v3::Filter as SyncFilter,
        },
        events::StateEventType,
        OwnedRoomId,
    },
    Client, RoomState,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;

const BRIDGE_EVENT_TYPES: [&str; 2] = ["m.bridge", "uk.half-shot.bridge"];

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SyncRequest {
    since: Option<String>,
    room_ids: Vec<String>,
}

#[derive(Debug)]
struct ValidatedSyncRequest {
    since: Option<String>,
    room_ids: Vec<OwnedRoomId>,
}

#[derive(Debug, Serialize)]
struct PortalAssertion {
    room_id: String,
    owner_user_id: String,
    joined: bool,
    bridge: Option<String>,
    active_portal: Option<bool>,
}

#[derive(Debug, Serialize)]
struct RoutedSyncEvent {
    room_id: String,
    event_id: String,
    sender: String,
    origin_server_ts: u64,
    event_type: String,
    message_type: Option<String>,
    body: Option<String>,
    decrypted: bool,
    live: bool,
    relation_type: Option<String>,
    redacted: bool,
}

#[derive(Debug, Serialize)]
struct RoutedSyncBatch {
    next_batch: String,
    events: Vec<RoutedSyncEvent>,
    rooms: Vec<PortalAssertion>,
}

fn read_sync_request(request_stdin: bool) -> Result<ValidatedSyncRequest> {
    if !request_stdin {
        bail!("refusing sync arguments; pass --request-stdin and provide JSON on standard input");
    }
    let mut raw = String::new();
    std::io::stdin()
        .read_to_string(&mut raw)
        .context("reading Matrix sync request from standard input")?;
    validate_sync_request(&raw)
}

fn validate_sync_request(raw: &str) -> Result<ValidatedSyncRequest> {
    let request: SyncRequest =
        serde_json::from_str(raw).context("parsing Matrix sync request JSON")?;
    if request.room_ids.is_empty() {
        bail!("Matrix sync request must contain at least one room ID");
    }
    if request.since.as_ref().is_some_and(|token| token.is_empty()) {
        bail!("Matrix sync token must be null or non-empty");
    }

    let mut seen = BTreeSet::new();
    let mut room_ids = Vec::with_capacity(request.room_ids.len());
    for raw_room_id in request.room_ids {
        let room_id = raw_room_id
            .parse::<OwnedRoomId>()
            .with_context(|| format!("parsing configured Matrix room ID {raw_room_id:?}"))?;
        if !seen.insert(room_id.clone()) {
            bail!("Matrix sync request contains duplicate room ID {room_id}");
        }
        room_ids.push(room_id);
    }
    Ok(ValidatedSyncRequest {
        since: request.since,
        room_ids,
    })
}

fn sync_settings(request: &ValidatedSyncRequest) -> SyncSettings {
    let mut timeline_filter = RoomEventFilter::default();
    timeline_filter.types = Some(vec![
        "m.room.message".to_owned(),
        "m.room.encrypted".to_owned(),
    ]);
    let mut room_filter = RoomFilter::default();
    room_filter.account_data = RoomEventFilter::ignore_all();
    room_filter.ephemeral = RoomEventFilter::ignore_all();
    room_filter.rooms = Some(request.room_ids.clone());
    room_filter.timeline = timeline_filter;
    let mut filter = FilterDefinition::default();
    filter.account_data = EventFilter::ignore_all();
    filter.presence = EventFilter::ignore_all();
    filter.room = room_filter;
    let token = match request.since.clone() {
        Some(token) => SyncToken::Specific(token),
        None => SyncToken::NoToken,
    };
    SyncSettings::default()
        .token(token)
        .timeout(Duration::from_secs(5))
        .filter(SyncFilter::FilterDefinition(filter))
}

fn raw_value(event: &TimelineEvent) -> Result<Value> {
    serde_json::from_str(event.raw().json().get())
        .context("parsing processed Matrix timeline event")
}

fn relation_type(content: &Value) -> Option<String> {
    let relation = content.get("m.relates_to")?;
    relation
        .get("rel_type")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .or_else(|| {
            relation
                .get("m.in_reply_to")
                .is_some()
                .then(|| "m.in_reply_to".to_owned())
        })
        .or_else(|| Some("unknown".to_owned()))
}

fn routed_event(room_id: &str, event: TimelineEvent, live: bool) -> Result<RoutedSyncEvent> {
    let value = raw_value(&event)?;
    let required_string = |field: &str| -> Result<String> {
        value
            .get(field)
            .and_then(Value::as_str)
            .filter(|item| !item.is_empty())
            .map(str::to_owned)
            .with_context(|| format!("Matrix timeline event omitted {field}"))
    };
    let content = value.get("content").unwrap_or(&Value::Null);
    let event_type = required_string("type")?;
    let decrypted = !matches!(event.kind, TimelineEventKind::UnableToDecrypt { .. });
    let redacted = value
        .pointer("/unsigned/redacted_because")
        .is_some_and(|redaction| !redaction.is_null());

    Ok(RoutedSyncEvent {
        room_id: room_id.to_owned(),
        event_id: required_string("event_id")?,
        sender: required_string("sender")?,
        origin_server_ts: value
            .get("origin_server_ts")
            .and_then(Value::as_u64)
            .context("Matrix timeline event omitted origin_server_ts")?,
        event_type,
        message_type: content
            .get("msgtype")
            .and_then(Value::as_str)
            .map(str::to_owned),
        body: content.get("body").and_then(Value::as_str).map(str::to_owned),
        decrypted,
        live,
        relation_type: relation_type(content),
        redacted,
    })
}

fn state_value(event: RawAnySyncOrStrippedState) -> Result<Value> {
    serde_json::to_value(event).context("serializing Matrix bridge state")
}

fn bridge_protocol(values: Vec<Value>) -> Result<Option<String>> {
    let mut assertions = BTreeSet::new();
    for value in values {
        if value
            .pointer("/unsigned/redacted_because")
            .is_some_and(|redaction| !redaction.is_null())
        {
            continue;
        }
        let content = value.get("content").unwrap_or(&Value::Null);
        let protocol = content.pointer("/protocol/id").and_then(Value::as_str);
        let channel = content.pointer("/channel/id").and_then(Value::as_str);
        let bridge_bot = content.get("bridgebot").and_then(Value::as_str);
        if let (Some(protocol), Some(channel), Some(bridge_bot)) =
            (protocol, channel, bridge_bot)
        {
            if !protocol.is_empty() && !channel.is_empty() && !bridge_bot.is_empty() {
                assertions.insert((
                    protocol.to_owned(),
                    channel.to_owned(),
                    bridge_bot.to_owned(),
                ));
                continue;
            }
        }
        bail!("Matrix room has malformed active bridge state");
    }
    if assertions.len() > 1 {
        bail!("Matrix room has conflicting active bridge assertions");
    }
    Ok(assertions.pop_first().map(|(protocol, _, _)| protocol))
}

async fn room_assertion(
    client: &Client,
    owner_user_id: &str,
    room_id: &OwnedRoomId,
) -> Result<PortalAssertion> {
    let Some(room) = client.get_room(room_id) else {
        return Ok(PortalAssertion {
            room_id: room_id.to_string(),
            owner_user_id: owner_user_id.to_owned(),
            joined: false,
            bridge: None,
            active_portal: Some(false),
        });
    };
    let joined = room.state() == RoomState::Joined;
    let mut bridge_values = Vec::new();
    if joined {
        for event_type in BRIDGE_EVENT_TYPES {
            let state = room
                .get_state_events(StateEventType::from(event_type))
                .await
                .with_context(|| format!("loading {event_type} state for room {room_id}"))?;
            for event in state {
                bridge_values.push(state_value(event)?);
            }
        }
    }
    let bridge = bridge_protocol(bridge_values)?;
    Ok(PortalAssertion {
        room_id: room_id.to_string(),
        owner_user_id: owner_user_id.to_owned(),
        joined,
        active_portal: Some(bridge.is_some()),
        bridge,
    })
}

async fn assertions(
    client: &Client,
    owner_user_id: &str,
    room_ids: &[OwnedRoomId],
) -> Result<Vec<PortalAssertion>> {
    let mut assertions = Vec::with_capacity(room_ids.len());
    for room_id in room_ids {
        assertions.push(room_assertion(client, owner_user_id, room_id).await?);
    }
    Ok(assertions)
}

pub async fn sync(client: &Client, owner_user_id: &str, request_stdin: bool) -> Result<()> {
    let request = read_sync_request(request_stdin)?;
    let live = request.since.is_some();
    let response = client
        .sync_once(sync_settings(&request))
        .await
        .context("synchronizing configured Matrix routes")?;
    if response.next_batch.is_empty() {
        bail!("Matrix sync returned an empty continuation token");
    }
    for room_id in response.rooms.iter_all_room_ids() {
        if !request.room_ids.contains(room_id) {
            bail!("Matrix sync returned an unconfigured room");
        }
    }
    let mut events = Vec::new();
    for (room_id, updates) in response.rooms.joined {
        if live && updates.timeline.limited {
            bail!("Matrix incremental sync timeline was limited; refusing to skip live events");
        }
        for event in updates.timeline.events {
            events.push(routed_event(room_id.as_str(), event, live)?);
        }
    }
    let rooms = assertions(client, owner_user_id, &request.room_ids).await?;
    println!(
        "{}",
        serde_json::to_string(&RoutedSyncBatch {
            next_batch: response.next_batch,
            events,
            rooms,
        })?
    );
    Ok(())
}

pub async fn room_status(client: &Client, owner_user_id: &str, room_id: String) -> Result<()> {
    let room_id = room_id
        .parse::<OwnedRoomId>()
        .context("parsing Matrix room ID")?;
    let request = ValidatedSyncRequest {
        since: None,
        room_ids: vec![room_id.clone()],
    };
    client
        .sync_once(sync_settings(&request))
        .await
        .context("refreshing Matrix room assertion")?;
    println!(
        "{}",
        serde_json::to_string(&room_assertion(client, owner_user_id, &room_id).await?)?
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use matrix_sdk::{
        deserialized_responses::TimelineEvent,
        ruma::{events::AnySyncTimelineEvent, serde::Raw},
    };
    use serde_json::{Value, json};

    use super::{bridge_protocol, routed_event, validate_sync_request};

    fn timeline_event(value: Value) -> TimelineEvent {
        let raw = Raw::<AnySyncTimelineEvent>::from_json_string(value.to_string()).unwrap();
        TimelineEvent::from_plaintext(raw)
    }

    #[test]
    fn sync_request_rejects_unknown_duplicate_and_malformed_rooms() {
        assert!(validate_sync_request(r#"{"room_ids":[],"since":null}"#).is_err());
        assert!(
            validate_sync_request(
                r#"{"room_ids":["!family:example.org","!family:example.org"],"since":null}"#
            )
            .is_err()
        );
        assert!(
            validate_sync_request(r#"{"room_ids":["family"],"since":null}"#).is_err()
        );
        assert!(
            validate_sync_request(
                r#"{"room_ids":["!family:example.org"],"since":null,"extra":true}"#
            )
            .is_err()
        );
    }

    #[test]
    fn initial_text_event_is_structured_but_not_live() {
        let event = timeline_event(json!({
            "type": "m.room.message",
            "event_id": "$one",
            "sender": "@friend:example.org",
            "origin_server_ts": 123,
            "content": {"msgtype": "m.text", "body": "hello"}
        }));
        let routed = routed_event("!family:example.org", event, false).unwrap();
        assert_eq!(routed.message_type.as_deref(), Some("m.text"));
        assert_eq!(routed.body.as_deref(), Some("hello"));
        assert!(routed.decrypted);
        assert!(!routed.live);
        assert!(routed.relation_type.is_none());
        assert!(!routed.redacted);
    }

    #[test]
    fn reply_and_redaction_metadata_cannot_look_like_original_text() {
        let event = timeline_event(json!({
            "type": "m.room.message",
            "event_id": "$reply",
            "sender": "@friend:example.org",
            "origin_server_ts": 456,
            "content": {
                "msgtype": "m.text",
                "body": "reply",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$one"}}
            },
            "unsigned": {"redacted_because": {"event_id": "$redaction"}}
        }));
        let routed = routed_event("!family:example.org", event, true).unwrap();
        assert_eq!(routed.relation_type.as_deref(), Some("m.in_reply_to"));
        assert!(routed.redacted);
    }

    #[test]
    fn portal_assertion_requires_structured_nonconflicting_bridge_state() {
        let valid = json!({
            "content": {
                "bridgebot": "@whatsappbot:example.org",
                "protocol": {"id": "whatsapp"},
                "channel": {"id": "15551234567"}
            }
        });
        assert_eq!(
            bridge_protocol(vec![valid.clone(), valid.clone()])
                .unwrap()
                .as_deref(),
            Some("whatsapp")
        );
        assert!(
            bridge_protocol(vec![
                valid.clone(),
                json!({"content": {"protocol": {"id": "signal"}}})
            ])
            .is_err()
        );
        assert!(
            bridge_protocol(vec![
                valid,
                json!({
                    "content": {
                        "bridgebot": "@signalbot:example.org",
                        "protocol": {"id": "signal"},
                        "channel": {"id": "signal-user"}
                    }
                })
            ])
            .is_err()
        );
    }
}
