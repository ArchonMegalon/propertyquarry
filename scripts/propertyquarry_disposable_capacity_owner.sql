\set ON_ERROR_STOP on

-- Disposable PostgreSQL lanes must establish the same ownership boundary that
-- schema v17 requires in production.  An existing role is accepted only when
-- it already has the exact fail-closed posture; an unsafe role is never
-- rewritten or adopted by a candidate test harness.
DO $propertyquarry_capacity_owner$
DECLARE
    owner_role RECORD;
BEGIN
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
    INTO owner_role
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = 'propertyquarry_admission_capacity_owner';

    IF NOT FOUND THEN
        CREATE ROLE propertyquarry_admission_capacity_owner WITH
            NOLOGIN
            NOINHERIT
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    ELSIF owner_role.rolcanlogin
       OR owner_role.rolinherit
       OR owner_role.rolsuper
       OR owner_role.rolcreaterole
       OR owner_role.rolcreatedb
       OR owner_role.rolreplication
       OR owner_role.rolbypassrls
       OR owner_role.memberships <> 0 THEN
        RAISE EXCEPTION 'propertyquarry admission capacity owner role is unsafe'
            USING ERRCODE = '42501';
    END IF;
END
$propertyquarry_capacity_owner$;

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
    )
FROM pg_catalog.pg_roles AS role
WHERE role.rolname = 'propertyquarry_admission_capacity_owner';
