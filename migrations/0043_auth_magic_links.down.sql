-- Down: drop auth_magic_links.
BEGIN;
DROP TABLE IF EXISTS mediator.auth_magic_links;
COMMIT;
