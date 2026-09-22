// Provision the least-privilege role the application runs as.
//
// This is what turns "the application never updates a ledger entry" into "the
// application's credentials cannot update a ledger entry". MongoDB's privilege
// actions are per collection, and `insert` is separate from `update` and
// `remove`, so an append-only grant is expressible directly.
//
// The distinction matters because it is the difference between a convention
// and a constraint. Without it, immutability rests on there being no code path
// that issues an update, which is true today and is one careless commit away
// from being false. With it, such a commit fails at runtime with Unauthorized.
//
// Deliberately NOT granted on `ledger_entries` or `transactions`:
//   update, remove, dropCollection, dropDatabase, bypassDocumentValidation
//
// `bypassDocumentValidation` is worth calling out: holding it would let the
// application write an entry that violates the $jsonSchema and $expr
// validators, which is the other half of the ledger's integrity.
//
// Idempotent, so it is safe to run on every start.

/* global db, print */

const databaseName = "ledgerlock";
const appUsername = process.env.LEDGERLOCK_MONGO_APP_USERNAME || "ledgerlock_app";
const appPassword = process.env.LEDGERLOCK_MONGO_APP_PASSWORD;

if (!appPassword) {
  throw new Error(
    "LEDGERLOCK_MONGO_APP_PASSWORD is not set; refusing to create a user " +
      "with an empty or guessable password"
  );
}

const target = db.getSiblingDB(databaseName);

// Everything needed to create collections, indexes and validators at startup,
// then read and append. No update. No remove.
const APPEND_ONLY_ACTIONS = [
  "find",
  "insert",
  "createCollection",
  "createIndex",
  "listIndexes",
  "listCollections",
  "collMod",
];

// The non-ledger collections are ordinary mutable state and need full CRUD.
// `accounts` in particular must be updatable: the debit serialisation counter
// is an $inc on the account document, and the overdraft guarantee depends on
// that write happening. Enumerated collection by collection rather than using
// the built-in `readWrite` role, because `readWrite` would cover
// `ledger_entries` too and quietly undo the whole point of this file.
const MUTABLE_ACTIONS = APPEND_ONLY_ACTIONS.concat(["update", "remove"]);

const APPEND_ONLY_COLLECTIONS = ["ledger_entries", "transactions"];
const MUTABLE_COLLECTIONS = ["users", "accounts", "revoked_tokens"];

const privileges = [];

for (const collection of APPEND_ONLY_COLLECTIONS) {
  privileges.push({
    resource: { db: databaseName, collection: collection },
    actions: APPEND_ONLY_ACTIONS,
  });
}
for (const collection of MUTABLE_COLLECTIONS) {
  privileges.push({
    resource: { db: databaseName, collection: collection },
    actions: MUTABLE_ACTIONS,
  });
}
// Database-level read of the collection catalogue, needed by
// `list_collection_names()` during startup schema setup.
privileges.push({
  resource: { db: databaseName, collection: "" },
  actions: ["listCollections", "listIndexes"],
});

const roleName = "ledgerlockAppendOnlyLedger";

const existingRole = target.getRole(roleName);
if (existingRole === null) {
  target.createRole({ role: roleName, privileges: privileges, roles: [] });
  print("[provision] created role " + roleName);
} else {
  // Re-apply, so tightening the grant takes effect on an existing deployment.
  target.updateRole(roleName, { privileges: privileges, roles: [] });
  print("[provision] updated role " + roleName);
}

const existingUser = target.getUser(appUsername);
if (existingUser === null) {
  target.createUser({
    user: appUsername,
    pwd: appPassword,
    roles: [{ role: roleName, db: databaseName }],
  });
  print("[provision] created user " + appUsername);
} else {
  target.updateUser(appUsername, {
    roles: [{ role: roleName, db: databaseName }],
  });
  print("[provision] updated user " + appUsername + " (roles only)");
}

print("[provision] append-only collections: " + APPEND_ONLY_COLLECTIONS.join(", "));
print("[provision] mutable collections:     " + MUTABLE_COLLECTIONS.join(", "));
print("[provision] done");
