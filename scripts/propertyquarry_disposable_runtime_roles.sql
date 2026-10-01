\set ON_ERROR_STOP on

-- PropertyQuarry schema migrations grant narrowly scoped table privileges to
-- these runtime roles. Disposable PostgreSQL lanes use the database owner for
-- their actual connections, but still need the same role boundary to exercise
-- the production migration contract. Existing roles are accepted only when
-- they already have the exact fail-closed posture.
DO $propertyquarry_runtime_roles$
DECLARE
    runtime_role_name TEXT;
    runtime_role RECORD;
BEGIN
    FOREACH runtime_role_name IN ARRAY ARRAY[
        'propertyquarry_api',
        'propertyquarry_scheduler',
        'propertyquarry_worker'
    ]
    LOOP
        SELECT
            role.rolcanlogin,
            role.rolinherit,
            role.rolsuper,
            role.rolcreaterole,
            role.rolcreatedb,
            role.rolreplication,
            role.rolbypassrls,
            (
                SELECT COUNT(*)
                FROM pg_catalog.pg_auth_members AS membership
                WHERE membership.member = role.oid
                   OR membership.roleid = role.oid
            ) AS memberships
        INTO runtime_role
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = runtime_role_name;

        IF NOT FOUND THEN
            EXECUTE format(
                'CREATE ROLE %I WITH NOLOGIN NOINHERIT NOSUPERUSER '
                'NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
                runtime_role_name
            );
        ELSIF runtime_role.rolcanlogin
           OR runtime_role.rolinherit
           OR runtime_role.rolsuper
           OR runtime_role.rolcreaterole
           OR runtime_role.rolcreatedb
           OR runtime_role.rolreplication
           OR runtime_role.rolbypassrls
           OR runtime_role.memberships <> 0 THEN
            RAISE EXCEPTION 'PropertyQuarry disposable runtime role is unsafe'
                USING ERRCODE = '42501';
        END IF;
    END LOOP;
END
$propertyquarry_runtime_roles$;

SELECT COUNT(*)
FROM pg_catalog.pg_roles AS role
WHERE role.rolname IN (
        'propertyquarry_api',
        'propertyquarry_scheduler',
        'propertyquarry_worker'
    )
  AND NOT role.rolcanlogin
  AND NOT role.rolinherit
  AND NOT role.rolsuper
  AND NOT role.rolcreaterole
  AND NOT role.rolcreatedb
  AND NOT role.rolreplication
  AND NOT role.rolbypassrls
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_auth_members AS membership
      WHERE membership.member = role.oid
         OR membership.roleid = role.oid
  );
