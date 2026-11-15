package main

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"filippo.io/age"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Synthetic SHOW CREATE shape from Kine v0.13.5's nine-column/index schema,
// surrounded by MySQL 8.4.10 mysqldump session/header/footer output. This is
// not a captured operational dump or qualification of a deployed DB schema.
func fixture() string {
	return "-- MySQL dump 10.13  Distrib 8.4.10, for Linux (x86_64)\n" +
		"-- Table structure for table `kine`\n" +
		"/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;\n" +
		"/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;\n" +
		"/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;\n" +
		"/*!50503 SET NAMES utf8mb4 */;\n" +
		"/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;\n" +
		"/*!40103 SET TIME_ZONE='+00:00' */;\n" +
		"/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;\n" +
		"/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;\n" +
		"/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;\n" +
		"/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;\n" +
		"/*!40101 SET @saved_cs_client     = @@character_set_client */;\n" +
		"/*!50503 SET character_set_client = utf8mb4 */;\n" +
		"CREATE TABLE `kine` (\n" +
		"  `id` bigint unsigned NOT NULL AUTO_INCREMENT,\n" +
		"  `name` varchar(630) CHARACTER SET ascii COLLATE ascii_general_ci DEFAULT NULL,\n" +
		"  `created` int DEFAULT NULL,\n" +
		"  `deleted` int DEFAULT NULL,\n" +
		"  `create_revision` bigint unsigned DEFAULT NULL,\n" +
		"  `prev_revision` bigint unsigned DEFAULT NULL,\n" +
		"  `lease` int DEFAULT NULL,\n" +
		"  `value` mediumblob,\n" +
		"  `old_value` mediumblob,\n" +
		"  PRIMARY KEY (`id`),\n" +
		"  UNIQUE KEY `kine_name_prev_revision_uindex` (`name`,`prev_revision`),\n" +
		"  KEY `kine_name_index` (`name`),\n" +
		"  KEY `kine_name_id_index` (`name`,`id`),\n" +
		"  KEY `kine_id_deleted_index` (`id`,`deleted`),\n" +
		"  KEY `kine_prev_revision_index` (`prev_revision`)\n" +
		") ENGINE=InnoDB AUTO_INCREMENT=2001 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;\n" +
		"/*!40101 SET character_set_client = @saved_cs_client */;\n" +
		"-- Dumping data for table `kine`\n" +
		"INSERT INTO `kine` (" + columns + ") VALUES (1,'compact_rev_key',1,0,1,504,0,NULL,NULL);\n" +
		"INSERT INTO `kine` (" + columns + ") VALUES (1504,'/registry/test,s,\\\\path',1,0,1504,0,0,0x000aff27,'');\n" +
		"/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;\n" +
		"/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;\n" +
		"/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;\n" +
		"/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;\n" +
		"/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;\n" +
		"/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;\n" +
		"/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;\n" +
		"/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;\n" +
		"-- Dump completed on 2026-11-11 00:00:00\n"
}
func TestProof(t *testing.T) {
	p, _, e := readDump(strings.NewReader(fixture()), io.Discard)
	if e != nil {
		t.Fatal(e)
	}
	if p.Rows != 2 || p.Revision != 1504 || p.CompactRevision != 504 || p.NextID != 2001 || len(p.TypedSHA) != 64 {
		t.Fatal(p)
	}
}
func TestRefuseBrokenDump(t *testing.T) {
	for _, s := range []string{strings.ReplaceAll(fixture(), "-- Dump completed on", "-- truncated"), strings.ReplaceAll(fixture(), "ENGINE=InnoDB", "ENGINE=MyISAM"), strings.ReplaceAll(fixture(), "AUTO_INCREMENT=2001", "AUTO_INCREMENT=2"), strings.ReplaceAll(fixture(), "VALUES (1504", "VALUES (1"), strings.ReplaceAll(fixture(), "0x000aff27", "'opaque'"), strings.ReplaceAll(fixture(), "compact_rev_key", "other"), "DROP TABLE `kine`;\n" + fixture(), strings.ReplaceAll(fixture(), "CREATE TABLE `kine`", "CREATE TABLE `other`")} {
		if _, _, e := readDump(strings.NewReader(s), io.Discard); e == nil {
			t.Fatal("accepted invalid dump")
		}
	}
}
func TestTypedNullAndEmptyDifferent(t *testing.T) {
	a, _, _ := readDump(strings.NewReader(fixture()), io.Discard)
	b, _, _ := readDump(strings.NewReader(strings.Replace(fixture(), "NULL,NULL", "NULL,''", 1)), io.Discard)
	if a.TypedSHA == b.TypedSHA {
		t.Fatal("NULL and empty BLOB merged")
	}
}
func TestOptions(t *testing.T) {
	c := config{Host: "127.0.0.1", Port: 13306, User: "read_only", Password: "a\\b\"c", Schema: "kine_canary_backup", CAFile: "/ca.pem", Recipients: []string{"a", "b"}}
	b, e := defaults(c)
	if e != nil || !bytes.Contains(b, []byte("ssl-mode=VERIFY_IDENTITY")) {
		t.Fatal(e)
	}
	for _, v := range []string{"a\nb", "a\rb", "a\x00b"} {
		c.Password = v
		if _, e = defaults(c); e == nil {
			t.Fatal("injected option")
		}
	}
}
func TestCaptureEncryptsOnlyAndBothRecipients(t *testing.T) {
	ids := make([]*age.X25519Identity, 2)
	var recipients []string
	for i := range ids {
		ids[i], _ = age.GenerateX25519Identity()
		recipients = append(recipients, ids[i].Recipient().String())
	}
	dir := t.TempDir()
	client := filepath.Join(dir, "mysqldump")
	script := "#!/bin/sh\nif [ \"$1\" = '--version' ]; then echo 'mysqldump Ver 8.4.10 for Linux'; exit 0; fi\ncat <<'DUMP'\n" + fixture() + "DUMP\n"
	os.WriteFile(client, []byte(script), 0700)
	c := config{Host: "127.0.0.1", Port: 13306, User: "reader", Password: "private-fixture", Schema: "kine_canary_backup", CAFile: "/fixture-ca", Recipients: recipients}
	b, _ := json.Marshal(c)
	cfg := filepath.Join(dir, "config.json")
	os.WriteFile(cfg, b, 0600)
	output := filepath.Join(dir, "ready")
	if e := capture(cfg, output, client); e != nil {
		t.Fatal(e)
	}
	files, _ := os.ReadDir(output)
	if len(files) != 2 {
		t.Fatal(files)
	}
	cipher, _ := os.ReadFile(filepath.Join(output, "k3s-mysql.sql.gz.age"))
	if bytes.Contains(cipher, []byte("INSERT INTO")) {
		t.Fatal("plaintext leaked")
	}
	for _, id := range ids {
		decrypted, e := age.Decrypt(bytes.NewReader(cipher), id)
		if e != nil {
			t.Fatal(e)
		}
		z, e := gzip.NewReader(decrypted)
		if e != nil {
			t.Fatal(e)
		}
		plain, e := io.ReadAll(z)
		if e != nil || string(plain) != fixture() {
			t.Fatal("roundtrip", e)
		}
	}
	if e := capture(cfg, output, client); e == nil {
		t.Fatal("overwrote existing output")
	}
	os.WriteFile(client, []byte(fmt.Sprintf("#!/bin/sh\nif [ \"$1\" = '--version' ]; then echo 'mysqldump Ver 8.4.10 for Linux'; exit 0; fi\nexit 1\n")), 0700)
	failed := filepath.Join(dir, "failed")
	if e := capture(cfg, failed, client); e == nil {
		t.Fatal("accepted client failure")
	}
	if _, e := os.Stat(failed); !os.IsNotExist(e) {
		t.Fatal("partial result retained")
	}
}

func TestOfficialClientEmptyBinaryIntroducer(t *testing.T) {
	original, _, err := readDump(strings.NewReader(fixture()), io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	actual, _, err := readDump(strings.NewReader(strings.ReplaceAll(fixture(), ",'');", ",_binary '');")), io.Discard)
	if err != nil || actual != original {
		t.Fatalf("empty binary literal changed typed proof: %v", err)
	}
	if _, _, err := readDump(strings.NewReader(strings.ReplaceAll(fixture(), ",'');", ",_binary 'raw');")), io.Discard); err == nil {
		t.Fatal("nonhex binary accepted")
	}
}

func TestRejectUnaccountedExecutableSQL(t *testing.T) {
	statements := []string{
		"DELETE FROM `kine`;", "UPDATE `kine` SET `value`=NULL;",
		"REPLACE INTO `kine` (`id`) VALUES (9);", "ALTER TABLE `kine` ADD `extra` int;",
		"RENAME TABLE `kine` TO `other`;", "TRUNCATE TABLE `kine`;", "DROP TABLE `kine`;",
		"CREATE TABLE `other` (`id` int);", "CREATE DATABASE `other`;", "USE `other`;",
		"LOCK TABLES `kine` WRITE;", "UNLOCK TABLES;", "SELECT 1;", "COMMIT;",
		"SET @arbitrary=1;", "/*!80000 DELETE FROM `kine` */;", "/*M! DELETE FROM kine */;",
		"/*!40101 SET SQL_MODE=CONCAT(@OLD_SQL_MODE,'NO_BACKSLASH_ESCAPES') */;",
		"/*!40101 SET SQL_MODE=@OLD_SQL_MODE */; DELETE FROM `kine`;",
		"/* comment */ DELETE FROM `kine`;", "/*+ SET_VAR(x=1) */ SELECT 1;",
		"DELIMITER $$", "source /private/file", "\\! echo forbidden", "--not-a-comment DELETE FROM kine;",
	}
	for i, statement := range statements {
		for name, sql := range map[string]string{
			"before":       statement + "\n" + fixture(),
			"between_rows": strings.Replace(fixture(), "INSERT INTO `kine` ("+columns+") VALUES (1504", statement+"\nINSERT INTO `kine` ("+columns+") VALUES (1504", 1),
			"after":        fixture() + statement + "\n",
		} {
			t.Run(fmt.Sprintf("%d/%s", i, name), func(t *testing.T) {
				if _, _, err := readDump(strings.NewReader(sql), io.Discard); err == nil {
					t.Fatal("unaccounted SQL acquired proof")
				}
			})
		}
	}
}
func TestRejectDDLAndCompletionAmbiguity(t *testing.T) {
	valid := fixture()
	cases := map[string]string{
		"missing_column":         strings.Replace(valid, "  `lease` int DEFAULT NULL,\n", "", 1),
		"extra_column":           strings.Replace(valid, "  PRIMARY KEY", "  `extra` int,\n  PRIMARY KEY", 1),
		"duplicate_column":       strings.Replace(valid, "  `lease` int DEFAULT NULL,", "  `lease` int DEFAULT NULL,\n  `lease` int DEFAULT NULL,", 1),
		"generated_column":       strings.Replace(valid, "`lease` int DEFAULT NULL", "`lease` int GENERATED ALWAYS AS (1)", 1),
		"column_expression":      strings.Replace(valid, "`lease` int DEFAULT NULL", "`lease` int DEFAULT (sleep(1))", 1),
		"unknown_index":          strings.Replace(valid, "KEY `kine_name_index` (`name`)", "KEY `unknown` (`name`)", 1),
		"wrong_index_columns":    strings.Replace(valid, "KEY `kine_name_index` (`name`)", "KEY `kine_name_index` (`lease`)", 1),
		"duplicate_index":        strings.Replace(valid, "  PRIMARY KEY (`id`),", "  PRIMARY KEY (`id`),\n  PRIMARY KEY (`id`),", 1),
		"missing_primary":        strings.Replace(valid, "  PRIMARY KEY (`id`),\n", "", 1),
		"missing_comma":          strings.Replace(valid, "`lease` int DEFAULT NULL,", "`lease` int DEFAULT NULL", 1),
		"trailing_comma":         strings.Replace(valid, "KEY `kine_prev_revision_index` (`prev_revision`)\n", "KEY `kine_prev_revision_index` (`prev_revision`),\n", 1),
		"table_option_tail":      strings.Replace(valid, "COLLATE=utf8mb4_0900_ai_ci;", "COLLATE=utf8mb4_0900_ai_ci; DELETE FROM kine;", 1),
		"unknown_option":         strings.Replace(valid, "ENGINE=InnoDB", "ENGINE=InnoDB DATA DIRECTORY='/tmp/x'", 1),
		"duplicate_create":       strings.Replace(valid, "-- Dumping data", "CREATE TABLE `kine` (\n-- Dumping data", 1),
		"insert_before_schema":   "INSERT INTO `kine` (" + columns + ") VALUES (1,'compact_rev_key',1,0,1,0,0,NULL,NULL);\n" + valid,
		"unfinished_create":      strings.Replace(valid, ") ENGINE=InnoDB", "-- ) ENGINE=InnoDB", 1),
		"invalid_date":           strings.Replace(valid, "2026-11-11 00:00:00", "2026-99-99 00:00:00", 1),
		"marker_not_terminal":    valid + "-- Dump completed on 2026-11-11 00:00:00\n",
		"marker_suffix":          strings.Replace(valid, "2026-11-11 00:00:00", "2026-11-11 00:00:00 SQL", 1),
		"fake_marker_in_comment": strings.Replace(valid, "-- Dump completed on 2026-11-11 00:00:00", "/*\n-- Dump completed on 2026-11-11 00:00:00\n*/", 1),
		"unterminated_comment":   valid[:strings.Index(valid, "-- Dump completed")] + "/* ordinary\n",
		"multiline_comment_tail": "/* ordinary\n*/ DELETE FROM kine;\n" + valid,
		"nested_comment":         "/* outer /* inner */ */\n" + valid,
	}
	for name, sql := range cases {
		t.Run(name, func(t *testing.T) {
			if _, _, err := readDump(strings.NewReader(sql), io.Discard); err == nil {
				t.Fatal("ambiguous dump acquired proof")
			}
		})
	}
}
func TestSupportedDDLAndOrdinaryCommentsPreserveTypedProof(t *testing.T) {
	want, _, err := readDump(strings.NewReader(fixture()), io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	for name, sql := range map[string]string{
		"ordinary_comments":       "# ordinary DELETE is inert\n/* standalone\nordinary UPDATE is inert\n*/\n--\n" + fixture() + "\n\n",
		"historical_signed":       strings.ReplaceAll(fixture(), "bigint unsigned", "bigint"),
		"ascii_default_collation": strings.ReplaceAll(fixture(), " COLLATE ascii_general_ci", ""),
		"latin1_table":            strings.ReplaceAll(fixture(), "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci", "DEFAULT CHARSET=latin1 COLLATE=latin1_swedish_ci"),
		"migration_id_unique":     strings.Replace(fixture(), "  PRIMARY KEY (`id`),", "  PRIMARY KEY (`id`),\n  UNIQUE KEY `id` (`id`),", 1),
		"minimal_options":         strings.ReplaceAll(fixture(), " DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci", ""),
		"escaped_SQL_is_data":     strings.Replace(fixture(), "'/registry/test,s,\\\\path'", "'/registry/DELETE; -- /* UPDATE */'", 1),
	} {
		t.Run(name, func(t *testing.T) {
			got, _, err := readDump(strings.NewReader(sql), io.Discard)
			if err != nil {
				t.Fatal(err)
			}
			if name != "escaped_SQL_is_data" && got != want {
				t.Fatal("non-row syntax changed typed framing")
			}
			if name == "escaped_SQL_is_data" && got.Rows != want.Rows {
				t.Fatal("SQL-looking quoted data was not preserved")
			}
		})
	}
}

func TestRejectMissingDuplicateOrReorderedSessionStatements(t *testing.T) {
	valid := fixture()
	for i, statement := range dumpSessionStatements {
		if strings.Count(valid, statement+"\n") != 1 {
			t.Fatal("fixture must independently contain each official session line exactly once")
		}
		for kind, sql := range map[string]string{
			"missing":             strings.Replace(valid, statement+"\n", "", 1),
			"duplicate":           strings.Replace(valid, statement+"\n", statement+"\n"+statement+"\n", 1),
			"moved_before_header": statement + "\n" + strings.Replace(valid, statement+"\n", "", 1),
		} {
			// Moving the already-first header statement before inert comments is legal.
			if i == 0 && kind == "moved_before_header" {
				continue
			}
			t.Run(fmt.Sprintf("%d/%s", i, kind), func(t *testing.T) {
				if _, _, err := readDump(strings.NewReader(sql), io.Discard); err == nil {
					t.Fatal("session envelope mutation accepted")
				}
			})
		}
	}
	for name, statement := range map[string]string{
		"sql_mode":       "/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;",
		"client_charset": "/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;",
		"table_charset":  "/*!40101 SET character_set_client = @saved_cs_client */;",
		"names":          "/*!50503 SET NAMES utf8mb4 */;",
	} {
		sql := strings.Replace(valid, statement+"\n", "", 1)
		// Explicit exploit: a known restore/setup moved into the data phase must
		// not authorize the following row under a different MySQL quote mode.
		first := strings.Index(sql, "INSERT INTO ")
		sql = sql[:first] + statement + "\n" + sql[first:]
		if name == "table_charset" { // expected position already immediately before rows
			second := strings.Index(sql, "INSERT INTO `kine` ("+columns+") VALUES (1504")
			sql = strings.Replace(valid, statement+"\n", "", 1)
			second = strings.Index(sql, "INSERT INTO `kine` ("+columns+") VALUES (1504")
			sql = sql[:second] + statement + "\n" + sql[second:]
		}
		t.Run("early_or_late_"+name, func(t *testing.T) {
			if _, _, err := readDump(strings.NewReader(sql), io.Discard); err == nil {
				t.Fatal("session statement relocated around row data")
			}
		})
	}
	// Even an otherwise valid row after a complete, correctly ordered footer is
	// refused before the terminal marker, rather than merely checked for order.
	row := "INSERT INTO `kine` (" + columns + ") VALUES (1505,'/later',1,0,1505,0,0,NULL,NULL);\n"
	sql := strings.Replace(valid, "-- Dump completed", row+"-- Dump completed", 1)
	if _, _, err := readDump(strings.NewReader(sql), io.Discard); err == nil {
		t.Fatal("row after footer accepted")
	}
}
func TestEveryKineIndexRequiredAndOrderPreserved(t *testing.T) {
	valid := fixture()
	for _, index := range []string{"PRIMARY KEY (`id`)", "KEY `kine_name_index` (`name`)", "KEY `kine_name_id_index` (`name`,`id`)", "KEY `kine_id_deleted_index` (`id`,`deleted`)", "KEY `kine_prev_revision_index` (`prev_revision`)", "UNIQUE KEY `kine_name_prev_revision_uindex` (`name`,`prev_revision`)"} {
		var lines []string
		removed := false
		for _, line := range strings.Split(valid, "\n") {
			if strings.TrimSuffix(strings.TrimSpace(line), ",") == index {
				removed = true
				continue
			}
			if strings.HasPrefix(line, ") ENGINE=") && len(lines) > 0 {
				lines[len(lines)-1] = strings.TrimSuffix(lines[len(lines)-1], ",")
			}
			lines = append(lines, line)
		}
		if !removed {
			t.Fatal("missing fixture index")
		}
		t.Run(index, func(t *testing.T) {
			if _, _, err := readDump(strings.NewReader(strings.Join(lines, "\n")), io.Discard); err == nil {
				t.Fatal("Kine index omitted without refusal")
			}
		})
	}
	// SHOW CREATE groups keys by server representation; key order is not row
	// identity. Reorder all six complete definitions without changing the proof.
	start := strings.Index(valid, "  PRIMARY KEY")
	end := strings.Index(valid, ") ENGINE=")
	keys := strings.Split(strings.TrimSpace(valid[start:end]), "\n")
	for i, j := 0, len(keys)-1; i < j; i, j = i+1, j-1 {
		keys[i], keys[j] = keys[j], keys[i]
	}
	for i := range keys {
		keys[i] = "  " + strings.TrimSuffix(strings.TrimSpace(keys[i]), ",")
		if i < len(keys)-1 {
			keys[i] += ","
		}
	}
	sql := valid[:start] + strings.Join(keys, "\n") + "\n" + valid[end:]
	want, _, err := readDump(strings.NewReader(valid), io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	got, _, err := readDump(strings.NewReader(sql), io.Discard)
	if err != nil || got != want {
		t.Fatalf("valid index reordering changed typed proof: %v", err)
	}
}
