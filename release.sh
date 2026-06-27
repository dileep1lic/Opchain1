#!/bin/bash

# सबसे नया टैग (version) ढूँढने की कोशिश करें
LATEST_TAG=$(git describe --tags --abbrev=0 2>/dev/null)

if [ -z "$LATEST_TAG" ]; then
    echo "📌 इस प्रोजेक्ट में अभी तक कोई वर्ज़न नहीं है। वर्ज़न v0.0.0 से शुरू माना जा रहा है।"
    LATEST_TAG="v0.0.0"
else
    echo "📌 वर्तमान वर्ज़न है: $LATEST_TAG"
fi

# चेक करें कि यूज़र ने इनपुट दिया है या नहीं
if [ $# -lt 2 ]; then
    echo "❌ इस्तेमाल करने का तरीका गलत है!"
    echo "✅ सही तरीका: ./release.sh <major|minor|patch> \"<कमिट मैसेज>\""
    echo "💡 उदाहरण 1 (छोटा बदलाव/बग फिक्स): ./release.sh patch \"बटन का रंग बदला\""
    echo "💡 उदाहरण 2 (नया फीचर): ./release.sh minor \"नया लॉगिन पेज जोड़ा\""
    echo "💡 उदाहरण 3 (बड़ा अपडेट): ./release.sh major \"नया डिज़ाइन लागू किया\""
    exit 1
fi

BUMP_TYPE=$1
MESSAGE=$2

# ── चेक करें कि कुछ staged (index में add) है या नहीं ──────────────
STAGED=$(git diff --cached --name-only)

if [ -z "$STAGED" ]; then
    echo ""
    echo "⚠️  कोई भी फाइल staged नहीं है!"
    echo "   पहले 'git add <file>' से फाइलें stage करें, फिर यह स्क्रिप्ट चलाएं।"
    echo ""
    echo "📋 Unstaged changes (अभी add नहीं हुईं):"
    git status --short
    exit 1
fi

echo ""
echo "📋 इन staged फाइलों को commit किया जाएगा:"
echo "$STAGED" | sed 's/^/   ✅ /'
echo ""

# वर्ज़न से 'v' हटाएँ (जैसे v1.2.3 से 1.2.3)
VERSION_NUM=${LATEST_TAG#v}

# वर्ज़न को 3 हिस्सों में तोड़ें: MAJOR, MINOR, PATCH
IFS='.' read -r -a PARTS <<< "$VERSION_NUM"
MAJOR=${PARTS[0]:-0}
MINOR=${PARTS[1]:-0}
PATCH=${PARTS[2]:-0}

# नया वर्ज़न कैलकुलेट करें
if [ "$BUMP_TYPE" == "major" ]; then
    MAJOR=$((MAJOR + 1))
    MINOR=0
    PATCH=0
elif [ "$BUMP_TYPE" == "minor" ]; then
    MINOR=$((MINOR + 1))
    PATCH=0
elif [ "$BUMP_TYPE" == "patch" ]; then
    PATCH=$((PATCH + 1))
else
    echo "❌ गलत वर्ज़न टाइप! कृपया 'major', 'minor', या 'patch' में से कोई एक टाइप करें।"
    exit 1
fi

NEW_VERSION="v$MAJOR.$MINOR.$PATCH"

echo "🚀 नया वर्ज़न $NEW_VERSION कैलकुलेट किया गया। रिलीज़ प्रोसेस शुरू हो रहा है..."

# 1. सिर्फ staged फाइलें commit करें (git add नहीं करेंगे)
git commit -m "$MESSAGE"
if [ $? -ne 0 ]; then
    echo "❌ Commit failed! कुछ गड़बड़ हुई।"
    exit 1
fi
echo "✅ Staged फाइलें commit हो गईं।"

# 2. नया टैग (वर्ज़न) बनाएँ
git tag -a "$NEW_VERSION" -m "Release $NEW_VERSION: $MESSAGE"
echo "✅ $NEW_VERSION टैग बन गया।"

# 3. कोड और टैग को गिटहब/सर्वर पर पुश करें
echo "⏳ सर्वर पर अपलोड (push) किया जा रहा है..."
git push
git push origin "$NEW_VERSION"

echo ""
echo "🎉 बधाई हो! $NEW_VERSION सफलतापूर्वक सेव और पुश हो गया है!"
