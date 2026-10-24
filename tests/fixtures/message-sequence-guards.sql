-- Complete constraint variant emitted by the pre-ticket-10 binary.
CREATE TRIGGER messages_sequence_insert
BEFORE INSERT ON messages
WHEN EXISTS(SELECT 1 FROM messages WHERE session_id = NEW.session_id AND seq = NEW.seq)
BEGIN SELECT RAISE(ABORT, 'duplicate message sequence'); END;
CREATE TRIGGER messages_sequence_update
BEFORE UPDATE OF session_id, seq ON messages
WHEN (NEW.session_id != OLD.session_id OR NEW.seq != OLD.seq)
  AND EXISTS(SELECT 1 FROM messages WHERE session_id = NEW.session_id
             AND seq = NEW.seq AND id != OLD.id)
BEGIN SELECT RAISE(ABORT, 'duplicate message sequence'); END;
