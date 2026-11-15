// k3s-mysql-backup streams a consistent Kine dump directly into gzip and age.
// No plaintext SQL, passwords, or row contents are emitted to logs.
package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"filippo.io/age"
	"flag"
	"fmt"
	"hash"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

const clientVersion = "8.4.10"
const image = "docker.io/library/mysql:8.4.10@sha256:8dbcf531a03aade657e181b9cf2f1d1803ce621a1d55610cb44cb531ab7d7db6"
const columns = "`id`, `name`, `created`, `deleted`, `create_revision`, `prev_revision`, `lease`, `value`, `old_value`"
const maxLine = 80 * 1024 * 1024
const maxCipher = 2 * 1024 * 1024 * 1024

// This is an allowlist for this helper's single-table MySQL 8.4.10 dump
// options, not a general SQL parser. mysqldump writes SHOW CREATE TABLE across
// lines and executable version comments for session setup/restore:
// https://github.com/mysql/mysql-server/blob/mysql-8.4.10/client/mysqldump.cc
// (write_header, write_footer, and get_table_structure). Unknown forms fail
// closed so unaccounted statements cannot acquire a qualified typed-row proof.
var tableOptions = regexp.MustCompile("^\\) ENGINE=InnoDB AUTO_INCREMENT=([1-9][0-9]*)(?: DEFAULT CHARSET=(?:utf8mb4|utf8mb3|latin1|ascii)(?: COLLATE=(?:utf8mb4|utf8mb3|latin1|ascii)_[a-z0-9_]+)?)?;$")
var dumpCompleted = regexp.MustCompile(`^-- Dump completed on [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$`)

// Kine v0.13.5 mysql.go schema/migration: fixed columns, safe historical
// signed-bigint spelling, and only the known index identities.
var kineColumnDefinitions = []*regexp.Regexp{
	regexp.MustCompile("^`id` bigint(?: unsigned)? NOT NULL AUTO_INCREMENT$"),
	regexp.MustCompile("^`name` varchar\\(630\\) CHARACTER SET ascii(?: COLLATE ascii_[a-z0-9_]+)? DEFAULT NULL$"),
	regexp.MustCompile("^`created` int DEFAULT NULL$"),
	regexp.MustCompile("^`deleted` int DEFAULT NULL$"),
	regexp.MustCompile("^`create_revision` bigint(?: unsigned)? DEFAULT NULL$"),
	regexp.MustCompile("^`prev_revision` bigint(?: unsigned)? DEFAULT NULL$"),
	regexp.MustCompile("^`lease` int DEFAULT NULL$"),
	regexp.MustCompile("^`value` mediumblob$"),
	regexp.MustCompile("^`old_value` mediumblob$"),
}
var kineIndexes = map[string]bool{
	"PRIMARY KEY (`id`)":                                                   true,
	"UNIQUE KEY `id` (`id`)":                                               true,
	"KEY `kine_name_index` (`name`)":                                       true,
	"KEY `kine_name_id_index` (`name`,`id`)":                               true,
	"KEY `kine_id_deleted_index` (`id`,`deleted`)":                         true,
	"KEY `kine_prev_revision_index` (`prev_revision`)":                     true,
	"UNIQUE KEY `kine_name_prev_revision_uindex` (`name`,`prev_revision`)": true,
}

// Keep exact, whole-line matches: /*!...*/ is executable SQL in MySQL, not
// an ordinary comment. In particular, do not allow arbitrary SET expressions.
var dumpSessionStatements = []string{
	"/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;",
	"/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;",
	"/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;",
	"/*!50503 SET NAMES utf8mb4 */;",
	"/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;",
	"/*!40103 SET TIME_ZONE='+00:00' */;",
	"/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;",
	"/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;",
	"/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;",
	"/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;",
	"/*!40101 SET @saved_cs_client     = @@character_set_client */;",
	"/*!50503 SET character_set_client = utf8mb4 */;",
	"/*!40101 SET character_set_client = @saved_cs_client */;",
	"/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;",
	"/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;",
	"/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;",
	"/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;",
	"/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;",
	"/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;",
	"/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;",
	"/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;",
}
var schemaName = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)

type config struct {
	Host       string   `json:"host"`
	Port       int      `json:"port"`
	User       string   `json:"user"`
	Password   string   `json:"password"`
	Schema     string   `json:"schema"`
	CAFile     string   `json:"ca_file"`
	Recipients []string `json:"recipients"`
}
type proof struct {
	Rows            uint64 `json:"rows"`
	Revision        uint64 `json:"revision"`
	CompactRevision uint64 `json:"compact_revision"`
	NextID          uint64 `json:"next_id"`
	TypedSHA        string `json:"typed_rows_sha256"`
}
type receipt struct {
	Status        string `json:"status"`
	Format        int    `json:"format_version"`
	Started       int64  `json:"started_epoch"`
	Completed     int64  `json:"completed_epoch"`
	Schema        string `json:"schema"`
	ClientVersion string `json:"client_version"`
	ClientImage   string `json:"client_image"`
	SQLSHA        string `json:"sql_sha256"`
	CipherSHA     string `json:"cipher_sha256"`
	CipherBytes   int64  `json:"cipher_bytes"`
	Proof         proof  `json:"proof"`
}
type parser struct {
	hash        hash.Hash
	proof       proof
	markers     int
	creates     int
	innodb      bool
	complete    bool
	inDDL       bool
	ddlColumn   int
	ddlComma    bool
	ddlIndexes  map[string]bool
	inComment   bool
	sessionStep int
}

func newParser() *parser {
	h := sha256.New()
	h.Write([]byte("kine-copy-typed-rows-v1\x00"))
	return &parser{hash: h}
}
func frame(h hash.Hash, tag byte, data []byte) {
	h.Write([]byte{tag})
	var size [8]byte
	binary.BigEndian.PutUint64(size[:], uint64(len(data)))
	h.Write(size[:])
	h.Write(data)
}
func splitValues(s string) ([]string, error) {
	var values []string
	start := 0
	quoted := false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if quoted {
			if c == '\\' {
				i++
				if i >= len(s) {
					return nil, errors.New("truncated escape")
				}
			} else if c == '\'' {
				if i+1 < len(s) && s[i+1] == '\'' {
					i++
				} else {
					quoted = false
				}
			}
		} else if c == '\'' {
			quoted = true
		} else if c == ',' {
			values = append(values, strings.TrimSpace(s[start:i]))
			start = i + 1
		}
	}
	if quoted {
		return nil, errors.New("unterminated value")
	}
	values = append(values, strings.TrimSpace(s[start:]))
	if len(values) != 9 {
		return nil, errors.New("unexpected column count")
	}
	return values, nil
}
func unquote(s string) ([]byte, error) {
	if len(s) < 2 || s[0] != '\'' || s[len(s)-1] != '\'' {
		return nil, errors.New("quoted value required")
	}
	s = s[1 : len(s)-1]
	out := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c == '\\' {
			i++
			if i >= len(s) {
				return nil, errors.New("truncated escape")
			}
			switch s[i] {
			case '0':
				c = 0
			case 'n':
				c = '\n'
			case 'r':
				c = '\r'
			case 't':
				c = '\t'
			case 'b':
				c = '\b'
			case 'Z':
				c = 26
			case '\\', '\'', '"':
				c = s[i]
			default:
				return nil, errors.New("unsupported SQL escape")
			}
		} else if c == '\'' {
			if i+1 < len(s) && s[i+1] == '\'' {
				i++
			} else {
				return nil, errors.New("unescaped quote")
			}
		}
		out = append(out, c)
	}
	return out, nil
}
func typed(s string, index int) (byte, []byte, error) {
	if s == "NULL" {
		return 'N', nil, nil
	}
	if index == 1 {
		b, e := unquote(s)
		if e == nil && !utf8.Valid(b) {
			e = errors.New("invalid key encoding")
		}
		return 'S', b, e
	}
	if index == 7 || index == 8 {
		// mysqldump 8.4 emits empty binary values as _binary '' even
		// with --hex-blob. Preserve the BLOB type and still reject raw data.
		if strings.HasPrefix(s, "_binary ") {
			s = strings.TrimPrefix(s, "_binary ")
		}
		if strings.HasPrefix(s, "0x") {
			b, e := hex.DecodeString(s[2:])
			return 'B', b, e
		}
		b, e := unquote(s)
		if e == nil && len(b) != 0 {
			e = errors.New("non-hex BLOB refused")
		}
		return 'B', b, e
	}
	n, e := strconv.ParseInt(s, 10, 64)
	if e != nil || strconv.FormatInt(n, 10) != s {
		return 0, nil, errors.New("noncanonical integer")
	}
	return 'I', []byte(s), nil
}

// ordinaryComment accepts only standalone, nonexecutable comments. SQL after
// a closing block-comment delimiter is refused instead of silently discarded.
func (p *parser) ordinaryComment(s string) (bool, error) {
	if p.inComment {
		if strings.Contains(s, "/*") {
			return false, errors.New("nested dump comment")
		}
		if end := strings.Index(s, "*/"); end >= 0 {
			if strings.TrimSpace(s[end+2:]) != "" {
				return false, errors.New("SQL after dump comment")
			}
			p.inComment = false
		}
		return true, nil
	}
	if s == "--" || strings.HasPrefix(s, "-- ") || strings.HasPrefix(s, "--\t") || strings.HasPrefix(s, "#") {
		return true, nil
	}
	if strings.HasPrefix(s, "/*M!") {
		return false, errors.New("unsupported executable comment")
	}
	if strings.HasPrefix(s, "/*") && !strings.HasPrefix(s, "/*!") && !strings.HasPrefix(s, "/*+") {
		rest := s[2:]
		if strings.Contains(rest, "/*") {
			return false, errors.New("nested dump comment")
		}
		if end := strings.Index(rest, "*/"); end >= 0 {
			if strings.TrimSpace(rest[end+2:]) != "" {
				return false, errors.New("SQL after dump comment")
			}
		} else {
			p.inComment = true
		}
		return true, nil
	}
	return false, nil
}
func (p *parser) ddlLine(s string) error {
	if strings.HasPrefix(s, ")") {
		m := tableOptions.FindStringSubmatch(s)
		if m == nil || p.ddlColumn != len(kineColumnDefinitions) || !p.ddlIndexes["PRIMARY KEY (`id`)"] || p.ddlComma {
			return errors.New("unsupported Kine table definition")
		}
		n, e := strconv.ParseUint(m[1], 10, 64)
		if e != nil {
			return errors.New("invalid sequence")
		}
		for index := range kineIndexes {
			if index != "UNIQUE KEY `id` (`id`)" && !p.ddlIndexes[index] {
				return errors.New("missing required Kine index")
			}
		}
		p.proof.NextID = n
		p.inDDL = false
		p.innodb = true
		return nil
	}
	if p.ddlColumn > 0 && !p.ddlComma {
		return errors.New("missing table definition comma")
	}
	item := strings.TrimSuffix(s, ",")
	p.ddlComma = item != s
	if p.ddlColumn < len(kineColumnDefinitions) {
		if !kineColumnDefinitions[p.ddlColumn].MatchString(item) {
			return errors.New("unsupported Kine column definition")
		}
		p.ddlColumn++
	} else {
		if !kineIndexes[item] || p.ddlIndexes[item] {
			return errors.New("unsupported or duplicate Kine index")
		}
		p.ddlIndexes[item] = true
	}
	return nil
}
func (p *parser) line(line string) error {
	s := strings.TrimSpace(line)
	if s == "" {
		return nil
	}
	// The completion marker is the last nonempty line from this pinned client.
	// Never let subsequent SQL (including a second insert) escape qualification.
	if p.complete {
		return errors.New("content after dump completion")
	}
	if !p.inComment && strings.HasPrefix(s, "-- Dump completed") {
		if p.inDDL || p.creates != 1 || !p.innodb || p.sessionStep != len(dumpSessionStatements) || !dumpCompleted.MatchString(s) {
			return errors.New("invalid dump completion")
		}
		if _, e := time.Parse("2006-01-02 15:04:05", strings.TrimPrefix(s, "-- Dump completed on ")); e != nil {
			return errors.New("invalid dump completion date")
		}
		p.complete = true
		return nil
	}
	if comment, e := p.ordinaryComment(s); comment || e != nil {
		return e
	}
	if p.inDDL {
		return p.ddlLine(s)
	}
	// Require the exact ordered session envelope emitted by this client's fixed
	// options: 10 header statements, 2 table-charset setup statements, CREATE,
	// table-charset restore, rows, then 8 footer statements. Early restores can
	// change quote interpretation, so matching an allowlisted SET alone is not
	// sufficient. No INSERT is accepted once the footer has started.
	if p.sessionStep < len(dumpSessionStatements) && s == dumpSessionStatements[p.sessionStep] {
		if p.sessionStep < 12 && p.creates != 0 || p.sessionStep == 12 && (!p.innodb || p.proof.Rows != 0) || p.sessionStep >= 13 && p.proof.Rows == 0 {
			return errors.New("misplaced dump session statement")
		}
		p.sessionStep++
		return nil
	}
	if s == "CREATE TABLE `kine` (" {
		if p.sessionStep != 12 || p.creates != 0 || p.proof.Rows != 0 {
			return errors.New("duplicate or misplaced Kine table")
		}
		p.creates++
		p.inDDL = true
		p.ddlIndexes = make(map[string]bool)
		return nil
	}
	if !strings.HasPrefix(s, "INSERT INTO ") || p.sessionStep != 13 || p.creates != 1 || !p.innodb {
		return errors.New("unsupported dump statement")
	}
	prefix := "INSERT INTO `kine` (" + columns + ") VALUES ("
	if !strings.HasPrefix(s, prefix) || !strings.HasSuffix(s, ");") {
		return errors.New("unexpected insert syntax")
	}
	vals, e := splitValues(s[len(prefix) : len(s)-2])
	if e != nil {
		return e
	}
	id, e := strconv.ParseUint(vals[0], 10, 64)
	if e != nil || id == 0 || id <= p.proof.Revision {
		return errors.New("nonascending revisions")
	}
	p.hash.Write([]byte{'R'})
	var size [8]byte
	binary.BigEndian.PutUint64(size[:], 9)
	p.hash.Write(size[:])
	for i, v := range vals {
		tag, data, e := typed(v, i)
		if e != nil {
			kind := "unrecognized"
			for _, prefix := range []string{"_binary", "_ascii", "_utf8mb4", "0x", "X'", "x'", "'", "NULL"} {
				if strings.HasPrefix(v, prefix) {
					kind = prefix
					break
				}
			}
			return fmt.Errorf("invalid column %d token class %s", i, kind)
		}
		frame(p.hash, tag, data)
		if i == 1 && string(data) == "compact_rev_key" {
			p.markers++
			n, e := strconv.ParseUint(vals[5], 10, 64)
			if e != nil {
				return errors.New("invalid compaction marker")
			}
			p.proof.CompactRevision = n
		}
	}
	p.proof.Revision = id
	p.proof.Rows++
	return nil
}
func (p *parser) finish() (proof, error) {
	if p.sessionStep != len(dumpSessionStatements) || p.inComment || p.inDDL || p.creates != 1 || !p.innodb || !p.complete || p.markers != 1 || p.proof.Rows == 0 || p.proof.CompactRevision > p.proof.Revision || p.proof.NextID <= p.proof.Revision {
		return proof{}, errors.New("incomplete Kine dump")
	}
	p.proof.TypedSHA = hex.EncodeToString(p.hash.Sum(nil))
	return p.proof, nil
}
func readDump(r io.Reader, w io.Writer) (proof, string, error) {
	p := newParser()
	h := sha256.New()
	scanner := bufio.NewScanner(io.TeeReader(r, io.MultiWriter(w, h)))
	scanner.Buffer(make([]byte, 128*1024), maxLine)
	for scanner.Scan() {
		if err := p.line(scanner.Text()); err != nil {
			return proof{}, "", err
		}
	}
	if err := scanner.Err(); err != nil {
		return proof{}, "", err
	}
	result, e := p.finish()
	return result, hex.EncodeToString(h.Sum(nil)), e
}
func option(s string) (string, error) {
	if strings.ContainsAny(s, "\x00\r\n") {
		return "", errors.New("invalid client option")
	}
	return "\"" + strings.NewReplacer("\\", "\\\\", "\"", "\\\"").Replace(s) + "\"", nil
}
func defaults(c config) ([]byte, error) {
	if c.Host != "127.0.0.1" || c.Port != 13306 || !schemaName.MatchString(c.Schema) || c.User == "" || c.Password == "" || !filepath.IsAbs(c.CAFile) || len(c.Recipients) < 2 {
		return nil, errors.New("invalid private configuration")
	}
	values := []string{"[client]", "protocol=TCP", "host=127.0.0.1", "port=13306", "ssl-mode=VERIFY_IDENTITY"}
	for _, entry := range [][2]string{{"user", c.User}, {"password", c.Password}, {"ssl-ca", c.CAFile}} {
		v, e := option(entry[1])
		if e != nil {
			return nil, e
		}
		values = append(values, entry[0]+"="+v)
	}
	return []byte(strings.Join(values, "\n") + "\n"), nil
}

// Keep bounded client diagnostics private; never copy them to container logs.
type diagnosticBuffer struct{ bytes.Buffer }

func (b *diagnosticBuffer) Write(p []byte) (int, error) {
	n := len(p)
	left := 16384 - b.Len()
	if left > 0 {
		if len(p) > left {
			p = p[:left]
		}
		b.Buffer.Write(p)
	}
	return n, nil
}

type limitedWriter struct {
	w   io.Writer
	n   int64
	dir string
}

func (w *limitedWriter) Write(b []byte) (int, error) {
	if w.n+int64(len(b)) > maxCipher {
		return 0, errors.New("encrypted output cap reached")
	}
	var fs syscall.Statfs_t
	if e := syscall.Statfs(w.dir, &fs); e != nil {
		return 0, e
	}
	if uint64(fs.Bavail)*uint64(fs.Bsize) < 128*1024*1024 {
		return 0, errors.New("capture free-space floor reached")
	}
	n, e := w.w.Write(b)
	w.n += int64(n)
	return n, e
}
func saveJSON(path string, v any) error {
	f, e := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return e
	}
	defer f.Close()
	if e = json.NewEncoder(f).Encode(v); e != nil {
		return e
	}
	return f.Sync()
}
func capture(configPath, output, client string) error {
	started := time.Now().Unix()
	st, e := os.Lstat(configPath)
	if e != nil || !st.Mode().IsRegular() || st.Mode().Perm()&0077 != 0 {
		return errors.New("private regular configuration required")
	}
	raw, e := os.ReadFile(configPath)
	if e != nil {
		return e
	}
	var c config
	d := json.NewDecoder(strings.NewReader(string(raw)))
	d.DisallowUnknownFields()
	if e = d.Decode(&c); e != nil {
		return e
	}
	settings, e := defaults(c)
	if e != nil {
		return e
	}
	var recipients []age.Recipient
	seen := map[string]bool{}
	for _, r := range c.Recipients {
		v, e := age.ParseX25519Recipient(r)
		if e != nil || seen[r] {
			return errors.New("unique age recipients required")
		}
		seen[r] = true
		recipients = append(recipients, v)
	}
	if e = os.Mkdir(output, 0700); e != nil {
		return e
	}
	success := false
	defer func() {
		if !success {
			os.RemoveAll(output)
		}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 600*time.Second)
	defer cancel()
	version, e := exec.CommandContext(ctx, client, "--version").Output()
	if e != nil || !strings.Contains(string(version), "Ver "+clientVersion+" ") {
		return errors.New("pinned mysqldump version required")
	}
	tmp, e := os.CreateTemp("", "kine-mysql-options-")
	if e != nil {
		return e
	}
	defer os.Remove(tmp.Name())
	if _, e = tmp.Write(settings); e != nil {
		tmp.Close()
		return e
	}
	if e = tmp.Close(); e != nil {
		return e
	}
	args := []string{"--defaults-file=" + tmp.Name(), "--single-transaction", "--quick", "--hex-blob", "--no-tablespaces", "--set-gtid-purged=OFF", "--column-statistics=0", "--skip-triggers", "--skip-lock-tables", "--skip-add-drop-table", "--skip-add-locks", "--skip-disable-keys", "--complete-insert", "--skip-extended-insert", "--order-by-primary", c.Schema, "kine"}
	cmd := exec.CommandContext(ctx, client, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	cmd.WaitDelay = 5 * time.Second
	diagnostics := &diagnosticBuffer{}
	cmd.Stderr = diagnostics
	stdout, e := cmd.StdoutPipe()
	if e != nil {
		return e
	}
	f, e := os.OpenFile(filepath.Join(output, "k3s-mysql.sql.gz.age"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return e
	}
	defer f.Close()
	h := sha256.New()
	bounded := &limitedWriter{w: io.MultiWriter(f, h), dir: output}
	encrypted, e := age.Encrypt(bounded, recipients...)
	if e != nil {
		return e
	}
	compressed := gzip.NewWriter(encrypted)
	if e = cmd.Start(); e != nil {
		return e
	}
	p, sqlSHA, readError := readDump(stdout, compressed)
	if readError != nil {
		cmd.Cancel()
	}
	waitError := cmd.Wait()
	zipError := compressed.Close()
	ageError := encrypted.Close()
	syncError := f.Sync()
	if readError != nil || waitError != nil || zipError != nil || ageError != nil || syncError != nil {
		reason := "client or output stream failed"
		if readError != nil {
			reason = readError.Error()
		}
		saveJSON(filepath.Join(filepath.Dir(output), "failure.private.json"), map[string]any{
			"status": "failed", "reason": reason, "client_diagnostic": diagnostics.String(),
			"client_failed": waitError != nil, "qualified": false})
		return errors.New("consistent encrypted dump failed")
	}
	r := receipt{Status: "qualified", Format: 1, Started: started, Completed: time.Now().Unix(), Schema: c.Schema, ClientVersion: clientVersion, ClientImage: image, SQLSHA: sqlSHA, CipherSHA: hex.EncodeToString(h.Sum(nil)), CipherBytes: bounded.n, Proof: p}
	if e = saveJSON(filepath.Join(output, "capture.json"), r); e != nil {
		return e
	}
	directory, e := os.Open(output)
	if e != nil {
		return e
	}
	e = directory.Sync()
	directory.Close()
	if e != nil {
		return e
	}
	success = true
	return nil
}

// Return only fixed error classifications, never paths, credentials or SQL.
func failureStage(err error) string {
	for _, message := range []string{"private regular configuration required", "invalid private configuration", "unique age recipients required", "pinned mysqldump version required", "consistent encrypted dump failed"} {
		if err.Error() == message {
			return message
		}
	}
	return "configuration or output I/O rejected"
}

func main() {
	configPath := flag.String("config", "/host/etc/k3s-mysql-backup/config.json", "Private configuration")
	output := flag.String("output", "/kine-capture/ready", "New output directory")
	client := flag.String("mysqldump", "/usr/bin/mysqldump", "Pinned client path")
	inspect := flag.String("inspect-sql", "", "Validate an offline SQL dump; never connect")
	flag.Parse()
	var e error
	if *inspect != "" {
		var f *os.File
		f, e = os.Open(*inspect)
		if e == nil {
			var p proof
			p, _, e = readDump(f, io.Discard)
			f.Close()
			if e == nil {
				e = json.NewEncoder(os.Stdout).Encode(p)
			}
		}
	} else {
		e = capture(*configPath, *output, *client)
	}
	if e != nil {
		fmt.Fprintf(os.Stderr, "Kine backup failed (%s); no qualified capture published\n", failureStage(e))
		os.Exit(1)
	}
}
