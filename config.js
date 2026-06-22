// Firebase web config for the SPX options viewer.
//
// This is PUBLIC client config and is safe to commit. Access is controlled by
// Firestore security rules (see firestore.rules): the website has read-only
// access; only the Tradier fetch script (Admin SDK) can write.
//
// Values come from: Firebase console -> Project settings -> General -> Your apps.
var firebaseConfig = {
  apiKey: "AIzaSyA3rT5k9GAoZivlrrTwGny7nQ3rOBHZm64",
  authDomain: "percent-strategy.firebaseapp.com",
  projectId: "percent-strategy",
  storageBucket: "percent-strategy.firebasestorage.app",
  messagingSenderId: "156903985014",
  appId: "1:156903985014:web:fa55871169f3e12af33457",
};
